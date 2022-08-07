import datetime
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from O365.utils import ApiComponent
from marshmallow import fields, post_load, pre_dump

from O365_notifications.utils import build_url, DeserializerSchema, Schema
from O365_notifications.constants import O365EventType, O365Namespace

__all__ = (
    "O365BaseNotification",
    "O365BaseSubscription",
    "O365Notification",
    "O365NotificationsHandler",
    "O365Subscriber",
)

logger = logging.getLogger(__name__)


@dataclass
class O365BaseNotification(ABC):
    type: O365Namespace.O365NotificationType
    raw: dict

    class BaseO365NotificationSchema(DeserializerSchema):
        type = fields.Str(data_key="@odata.type")

        def __init__(self, **kwargs):
            self.namespace = kwargs.pop("namespace", None)
            super().__init__(**kwargs)

        @post_load
        def post_load(self, data, **_):
            data["type"] = self.namespace.O365NotificationType(data["type"])
            return O365BaseNotification(**data)

    schema = BaseO365NotificationSchema  # alias

    @classmethod
    def deserialize(cls, data: dict, **kwargs):
        return cls.schema(**kwargs).load(data)


@dataclass
class O365Notification(O365BaseNotification):
    id: str
    subscription_id: str
    subscription_expire: datetime
    sequence: int
    event: O365EventType

    @dataclass
    class O365ResourceData:
        type: O365Namespace.O365ResourceDataType
        url: str
        etag: str
        id: str

    class O365NotificationSchema(O365BaseNotification.schema):
        id = fields.Str(data_key="Id")
        subscription_id = fields.Str(data_key="SubscriptionId")
        subscription_expire = fields.DateTime(data_key="SubscriptionExpirationDateTime")
        sequence = fields.Int(data_key="SequenceNumber")
        event = fields.Str(data_key="ChangeType")
        resource = fields.Nested(
            Schema.from_dict(
                {
                    "type": fields.Str(data_key="@odata.type"),
                    "url": fields.Url(data_key="@odata.id"),
                    "etag": fields.Str(data_key="@odata.etag"),
                    "id": fields.Str(data_key="Id"),
                }
            ),
            data_key="ResourceData",
        )

        @post_load
        def post_load(self, data, **_):
            data["type"] = self.namespace.O365NotificationType.NOTIFICATION
            data["event"] = O365EventType(data["event"])
            data["resource"]["type"] = self.namespace.O365ResourceDataType(
                data["resource"]["type"]
            )
            return O365Notification(**data)

    resource: O365ResourceData
    schema = O365NotificationSchema  # alias


@dataclass
class O365BaseSubscription(ABC):
    type: O365Namespace.O365SubscriptionType
    resource_url: str
    events: list[O365EventType]
    id: str = None
    raw: dict = None

    class BaseO365SubscriptionSchema(Schema):
        id = fields.Str(data_key="Id", load_only=True)
        type = fields.Str(data_key="@odata.type")
        resource_url = fields.Str(data_key="Resource")
        events = fields.Str(data_key="ChangeType")

        def __init__(self, **kwargs):
            self.namespace = kwargs.pop("namespace", None)
            super().__init__(**kwargs)

        @pre_dump
        def pre_dump(self, obj, **_):
            data = obj.__dict__
            data["type"] = data["type"].value
            data["events"] = ",".join(e.value for e in data["events"])
            return data

        @post_load
        def post_load(self, data, **_):
            data["type"] = self.namespace.O365SubscriptionType(data["type"])
            data["events"] = [O365EventType(e) for e in data["events"].split(",")]
            return O365BaseSubscription(**{**data, "raw": data})

    schema = BaseO365SubscriptionSchema  # alias

    @classmethod
    def deserialize(cls, data: dict, **kwargs):
        return cls.schema(**kwargs).load(data)

    def serialize(self, **kwargs):
        return self.schema(**kwargs).dump(self)


class O365Subscriber(ApiComponent, ABC):
    _endpoints = {"subscriptions": "/subscriptions"}
    subscription_cls = O365BaseSubscription

    def __init__(self, *, parent=None, con=None, **kwargs):
        protocol = kwargs.get("protocol", getattr(parent, "protocol", None))
        main_resource = kwargs.get(
            "main_resource", getattr(parent, "main_resource", None)
        )

        super().__init__(protocol=protocol, main_resource=main_resource)

        self.con = getattr(parent, "con", con)  # communication with the api provider
        self.namespace = O365Namespace.from_protocol(protocol=protocol)
        self.subscriptions = []

    def subscription_factory(self, **kwargs) -> O365BaseSubscription:
        return self.subscription_cls(**kwargs)

    def subscribe(self, *, resource: ApiComponent, events: list[O365EventType]):
        """
        Subscription to a given resource.

        :param resource: the resource to subscribe to
        :param events: events type for the resource subscription
        """
        update = next((s for s in self.subscriptions if s.resource == resource), None)
        if update:
            events = [ev for ev in events if ev not in update.events]
            if not events:
                raise ValueError("subscription for given resource already exists")

        req = self.subscription_factory(
            resource_url=build_url(resource), events=events
        ).serialize()

        url = self.build_url(self._endpoints.get("subscriptions"))
        response = self.con.post(url, req)
        raw = response.json()

        # register subscription
        subscription = self.subscription_cls.deserialize(
            data=raw, namespace=self.namespace
        )
        if update:
            update.id = subscription.id
            update.events.append(events)
            update.raw = raw
        else:
            self.subscriptions.append(subscription)
        logger.debug(f"Subscribed to resource '{resource}' on events: '{events}'")

    def renew_subscriptions(self):
        names = ", ".join(f"'{s.resource}'" for s in self.subscriptions)
        logger.info(f"Renewing subscriptions for {names} ...")
        map(
            lambda s: self.subscribe(resource=s.resource, events=s.events),
            self.subscriptions,
        )
        logger.info("Subscriptions renewed.")


class O365NotificationsHandler(ABC):
    @abstractmethod
    def process(self, notification: O365BaseNotification):
        logger.debug(vars(notification))
