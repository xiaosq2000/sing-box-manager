from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ConnectionEventType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CONNECTION_EVENT_NEW: _ClassVar[ConnectionEventType]
    CONNECTION_EVENT_UPDATE: _ClassVar[ConnectionEventType]
    CONNECTION_EVENT_CLOSED: _ClassVar[ConnectionEventType]
CONNECTION_EVENT_NEW: ConnectionEventType
CONNECTION_EVENT_UPDATE: ConnectionEventType
CONNECTION_EVENT_CLOSED: ConnectionEventType

class SubscribeConnectionsRequest(_message.Message):
    __slots__ = ("interval",)
    INTERVAL_FIELD_NUMBER: _ClassVar[int]
    interval: int
    def __init__(self, interval: _Optional[int] = ...) -> None: ...

class ConnectionEvent(_message.Message):
    __slots__ = ("type", "id", "connection", "uplinkDelta", "downlinkDelta", "closedAt")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_FIELD_NUMBER: _ClassVar[int]
    UPLINKDELTA_FIELD_NUMBER: _ClassVar[int]
    DOWNLINKDELTA_FIELD_NUMBER: _ClassVar[int]
    CLOSEDAT_FIELD_NUMBER: _ClassVar[int]
    type: ConnectionEventType
    id: str
    connection: Connection
    uplinkDelta: int
    downlinkDelta: int
    closedAt: int
    def __init__(self, type: _Optional[_Union[ConnectionEventType, str]] = ..., id: _Optional[str] = ..., connection: _Optional[_Union[Connection, _Mapping]] = ..., uplinkDelta: _Optional[int] = ..., downlinkDelta: _Optional[int] = ..., closedAt: _Optional[int] = ...) -> None: ...

class ConnectionEvents(_message.Message):
    __slots__ = ("events", "reset")
    EVENTS_FIELD_NUMBER: _ClassVar[int]
    RESET_FIELD_NUMBER: _ClassVar[int]
    events: _containers.RepeatedCompositeFieldContainer[ConnectionEvent]
    reset: bool
    def __init__(self, events: _Optional[_Iterable[_Union[ConnectionEvent, _Mapping]]] = ..., reset: _Optional[bool] = ...) -> None: ...

class Connection(_message.Message):
    __slots__ = ("id", "inbound", "inboundType", "ipVersion", "network", "source", "destination", "domain", "protocol", "user", "fromOutbound", "createdAt", "closedAt", "uplink", "downlink", "uplinkTotal", "downlinkTotal", "rule", "outbound", "outboundType", "chainList", "processInfo")
    ID_FIELD_NUMBER: _ClassVar[int]
    INBOUND_FIELD_NUMBER: _ClassVar[int]
    INBOUNDTYPE_FIELD_NUMBER: _ClassVar[int]
    IPVERSION_FIELD_NUMBER: _ClassVar[int]
    NETWORK_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    DESTINATION_FIELD_NUMBER: _ClassVar[int]
    DOMAIN_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    FROMOUTBOUND_FIELD_NUMBER: _ClassVar[int]
    CREATEDAT_FIELD_NUMBER: _ClassVar[int]
    CLOSEDAT_FIELD_NUMBER: _ClassVar[int]
    UPLINK_FIELD_NUMBER: _ClassVar[int]
    DOWNLINK_FIELD_NUMBER: _ClassVar[int]
    UPLINKTOTAL_FIELD_NUMBER: _ClassVar[int]
    DOWNLINKTOTAL_FIELD_NUMBER: _ClassVar[int]
    RULE_FIELD_NUMBER: _ClassVar[int]
    OUTBOUND_FIELD_NUMBER: _ClassVar[int]
    OUTBOUNDTYPE_FIELD_NUMBER: _ClassVar[int]
    CHAINLIST_FIELD_NUMBER: _ClassVar[int]
    PROCESSINFO_FIELD_NUMBER: _ClassVar[int]
    id: str
    inbound: str
    inboundType: str
    ipVersion: int
    network: str
    source: str
    destination: str
    domain: str
    protocol: str
    user: str
    fromOutbound: str
    createdAt: int
    closedAt: int
    uplink: int
    downlink: int
    uplinkTotal: int
    downlinkTotal: int
    rule: str
    outbound: str
    outboundType: str
    chainList: _containers.RepeatedScalarFieldContainer[str]
    processInfo: ProcessInfo
    def __init__(self, id: _Optional[str] = ..., inbound: _Optional[str] = ..., inboundType: _Optional[str] = ..., ipVersion: _Optional[int] = ..., network: _Optional[str] = ..., source: _Optional[str] = ..., destination: _Optional[str] = ..., domain: _Optional[str] = ..., protocol: _Optional[str] = ..., user: _Optional[str] = ..., fromOutbound: _Optional[str] = ..., createdAt: _Optional[int] = ..., closedAt: _Optional[int] = ..., uplink: _Optional[int] = ..., downlink: _Optional[int] = ..., uplinkTotal: _Optional[int] = ..., downlinkTotal: _Optional[int] = ..., rule: _Optional[str] = ..., outbound: _Optional[str] = ..., outboundType: _Optional[str] = ..., chainList: _Optional[_Iterable[str]] = ..., processInfo: _Optional[_Union[ProcessInfo, _Mapping]] = ...) -> None: ...

class ProcessInfo(_message.Message):
    __slots__ = ("processId", "userId", "userName", "processPath", "packageNames")
    PROCESSID_FIELD_NUMBER: _ClassVar[int]
    USERID_FIELD_NUMBER: _ClassVar[int]
    USERNAME_FIELD_NUMBER: _ClassVar[int]
    PROCESSPATH_FIELD_NUMBER: _ClassVar[int]
    PACKAGENAMES_FIELD_NUMBER: _ClassVar[int]
    processId: int
    userId: int
    userName: str
    processPath: str
    packageNames: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, processId: _Optional[int] = ..., userId: _Optional[int] = ..., userName: _Optional[str] = ..., processPath: _Optional[str] = ..., packageNames: _Optional[_Iterable[str]] = ...) -> None: ...
