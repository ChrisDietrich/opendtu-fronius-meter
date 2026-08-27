#!/usr/bin/env python3
"""
opendtu_fronius_meter.py

Author: Chris Dietrich

Emulates a single Fronius-compatible SunSpec Modbus TCP meter (Common
Model 1 + Float Meter Model 213), fed live from one or more OpenDTU
inverters' MQTT data, combined into one virtual meter.

Why one combined meter rather than one virtual meter per inverter (which
an earlier sibling project, hoymiles-fronius-meter, tried first): a real
Fronius GEN24's "add meter" dialog rejects a second Modbus TCP meter
entry that shares an IP address with an existing one -- regardless of
port. Since a Home Assistant host normally has one address, that makes
"one virtual meter per inverter" a dead end without extra networking
(a second IP alias, a second NIC, ...). Aggregating every inverter into
ONE virtual meter sidesteps the problem entirely: GEN24 only ever sees
one Modbus TCP meter to register, at one IP:port.

Each configured inverter still gets attributed to its own wired phase
(L1/L2/L3) within that one meter's 3-phase register set -- see
combine_phase() -- rather than collapsing everything onto a single phase
regardless of which leg each inverter is actually wired to.

Register layout is SunSpec-standard (Common model 1, Meter model 213 in
"float" representation), copied from and verified in the sibling
hoymiles-fronius-meter project against real GEN24 hardware and a real
pymodbus client. Real GEN24 units are known to be picky about response
latency (see DOCS.md) -- this script keeps the MQTT I/O and the register
updates off the request/response path so replies stay fast.

Configuration is via environment variables -- see README.md / DOCS.md, or
config.yaml's options/schema if running as a Home Assistant Supervisor App
(entrypoint.py translates those into the env vars this file reads):

- Global settings (MQTT connection, update interval, log level, the one
  Modbus TCP port, and the one virtual meter's SunSpec identity) are
  plain flat env vars -- see GlobalConfig below.
- Per-inverter settings (topics, wired phase) arrive as a single JSON
  array in INVERTERS_JSON, one object per inverter -- see
  InverterSource/load_inverters() below. entrypoint.py builds this from
  config.yaml's `inverters` list-of-dicts option.
"""

import json
import logging
import math
import os
import struct
import sys
import threading
from dataclasses import dataclass, field
from typing import NamedTuple

import paho.mqtt.client as mqtt
from pymodbus.datastore import ModbusSlaveContext, ModbusSparseDataBlock, ModbusServerContext
from pymodbus.server import StartTcpServer

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("opendtu_fronius_meter")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def _split_topics(value):
    if isinstance(value, list):
        return value
    return [t.strip() for t in str(value).split(",") if t.strip()]


def _topic_matches(topic: str, pattern: str) -> bool:
    """MQTT topic-filter matching for the single-level '+' wildcard (we
    don't need '#' -- base-topic derivation only ever produces one-level
    wildcards, and arbitrary custom overrides are expected to be literal
    topics). A pattern with no wildcard segments just needs an exact
    match, so this also correctly handles today's plain literal-topic
    configs without any special-casing."""
    topic_parts = topic.split("/")
    pattern_parts = pattern.split("/")
    if len(topic_parts) != len(pattern_parts):
        return False
    return all(p == "+" or p == t for p, t in zip(pattern_parts, topic_parts))


@dataclass
class GlobalConfig:
    """Settings for the one combined virtual meter this process serves --
    one MQTT connection, one Modbus TCP listener, one SunSpec identity,
    regardless of how many inverters feed into it."""
    mqtt_host: str = os.environ.get("MQTT_HOST", "localhost")
    mqtt_port: int = int(os.environ.get("MQTT_PORT", "1883"))
    mqtt_username: str = os.environ.get("MQTT_USERNAME", "")
    mqtt_password: str = os.environ.get("MQTT_PASSWORD", "")
    update_interval: float = float(os.environ.get("UPDATE_INTERVAL_SECONDS", "2"))

    listen_host: str = os.environ.get("TCP_LISTEN_HOST", "0.0.0.0")
    # Fronius GEN24's "add meter" dialog only offers a fixed choice of 502
    # or 1502 -- pick whichever isn't already used by the primary meter or
    # any other additional meter you register.
    listen_port: int = int(os.environ.get("TCP_LISTEN_PORT", "1502"))

    meter_manufacturer: str = os.environ.get("METER_MANUFACTURER", "Hoymiles")
    meter_model: str = os.environ.get("METER_MODEL", "OpenDTU Virtual Meter")
    meter_serial: str = os.environ.get("METER_SERIAL", "OTU00001")
    # "Device address" field inside the Common model. Fronius uses 240 for
    # the primary meter internally; pick something distinct (241, 242, ...)
    # if you also run other additional/virtual meters.
    meter_device_address: int = int(os.environ.get("METER_DEVICE_ADDRESS", "241"))


@dataclass
class InverterSource:
    """One inverter's worth of MQTT topics feeding the combined meter,
    plus which physical phase (1=L1, 2=L2, 3=L3) its AC output is wired
    to. One of these is built per entry in INVERTERS_JSON -- see
    load_inverters(). Multiple sources may share a phase (their
    contributions are combined -- see combine_phase()); they don't need
    to be on different phases.

    Topic fields accept either a comma-separated string (as they arrive
    from JSON/HA config) or an already-split list (convenient for tests
    constructing this directly); __post_init__ normalizes to a list either
    way.
    """
    mqtt_base_topic: str = ""
    power_topic: list = field(default_factory=list)
    energy_topic: list = field(default_factory=list)
    energy_scale: float = 1000.0
    voltage_topic: list = field(default_factory=list)
    current_topic: list = field(default_factory=list)
    frequency_topic: list = field(default_factory=list)
    power_factor_topic: list = field(default_factory=list)
    reactive_power_topic: list = field(default_factory=list)

    # Which physical phase (1=L1, 2=L2, 3=L3) this inverter's AC output is
    # wired to. Hoymiles/OpenDTU-managed microinverters are single-phase,
    # but Model 213 is a 3-phase meter -- see combine_phase().
    meter_phase: int = 1

    def __post_init__(self):
        self.energy_scale = float(self.energy_scale)
        self.meter_phase = int(self.meter_phase)
        if self.meter_phase not in (1, 2, 3):
            raise ValueError(f"meter_phase must be 1, 2 or 3, got {self.meter_phase!r}")
        self.power_topic = _split_topics(self.power_topic)
        self.energy_topic = _split_topics(self.energy_topic)
        self.voltage_topic = _split_topics(self.voltage_topic)
        self.current_topic = _split_topics(self.current_topic)
        self.frequency_topic = _split_topics(self.frequency_topic)
        self.power_factor_topic = _split_topics(self.power_factor_topic)
        self.reactive_power_topic = _split_topics(self.reactive_power_topic)
        if self.mqtt_base_topic:
            self._derive_topics_from_base()

    def _derive_topics_from_base(self):
        """Fills in any of the seven topic lists left unset, from
        mqtt_base_topic, using OpenDTU's fixed topic layout (see
        README.md's recorded sample traffic):

        - power/energy are published once per OpenDTU instance, at the
          base topic itself (no serial needed) -- "<base>/ac/power" etc.
        - voltage/current/frequency/power-factor/reactive-power are
          published per inverter *channel*, under the inverter's serial
          number -- "<base>/<serial>/0/voltage" etc. Rather than requiring
          the user to look up that serial, we subscribe with MQTT's `+`
          single-level wildcard ("<base>/+/0/voltage"): the broker matches
          whichever serial is actually publishing under this base topic.
          This assumes one inverter per OpenDTU instance/base topic (the
          common case) -- if that's ever not true, an explicit topic
          override still takes precedence (see the field defs above).
        """
        b = self.mqtt_base_topic
        if not self.power_topic:
            self.power_topic = [f"{b}/ac/power"]
        if not self.energy_topic:
            self.energy_topic = [f"{b}/ac/yieldtotal"]
        if not self.voltage_topic:
            self.voltage_topic = [f"{b}/+/0/voltage"]
        if not self.current_topic:
            self.current_topic = [f"{b}/+/0/current"]
        if not self.frequency_topic:
            self.frequency_topic = [f"{b}/+/0/frequency"]
        if not self.power_factor_topic:
            self.power_factor_topic = [f"{b}/+/0/powerfactor"]
        if not self.reactive_power_topic:
            self.reactive_power_topic = [f"{b}/+/0/reactivepower"]

    def label(self) -> str:
        return f"{self.mqtt_base_topic or 'custom topics'} (phase L{self.meter_phase})"


def load_inverters() -> list:
    """Reads INVERTERS_JSON -- a JSON array of per-inverter option dicts,
    one per OpenDTU inverter feeding the combined meter (see entrypoint.py,
    which builds this from config.yaml's `inverters` list-of-dicts option)
    -- and returns one InverterSource per entry.
    """
    raw = os.environ.get("INVERTERS_JSON", "")
    if not raw:
        return []
    items = json.loads(raw)
    sources = []
    for item in items:
        # Options the user left blank arrive as None/"" from HA's config
        # UI (or may simply be absent) -- dropping None here lets
        # InverterSource's own field defaults apply, same as
        # entrypoint.py's OPTION_TO_ENV translation already does for
        # global options.
        cleaned = {k: v for k, v in item.items() if v is not None}
        sources.append(InverterSource(**cleaned))
    return sources


def validate_inverters(inverters: list):
    if not inverters:
        log.error(
            "No inverters configured (INVERTERS_JSON is empty) -- nothing to serve. Exiting.")
        sys.exit(1)
    for src in inverters:
        if not src.power_topic:
            log.error(
                "Inverter %s has no power topic configured (set an OpenDTU "
                "base topic or an explicit power topic override). Exiting.",
                src.label())
            sys.exit(1)
        if not src.voltage_topic or not src.current_topic:
            log.warning(
                "Inverter %s: voltage/current topics not fully set -- "
                "this inverter's contribution to phase L%d voltage/current "
                "will be 0 (power/energy are unaffected).",
                src.label(), src.meter_phase)


# --------------------------------------------------------------------------
# SunSpec float32 register helpers (verified against a real pymodbus client)
# --------------------------------------------------------------------------

def float_to_registers(value: float):
    """IEEE-754 float32, big-endian, split into two 16-bit registers
    (high word, low word) -- the standard SunSpec float encoding."""
    packed = struct.pack('>f', float(value))
    hi, lo = struct.unpack('>HH', packed)
    return hi, lo


def _ascii_to_registers(text: str, reg_count: int):
    """Pack an ASCII string into reg_count 16-bit registers, 2 chars/register,
    null-padded -- the SunSpec 'string' encoding used in the Common model."""
    raw = text.encode('ascii', errors='replace')[: reg_count * 2]
    raw = raw.ljust(reg_count * 2, b'\x00')
    return [
        (raw[i] << 8) | raw[i + 1]
        for i in range(0, len(raw), 2)
    ]


# Register offsets (0-based, within the 124-register Model-213 payload) per
# the official SunSpec model definition (sunspec/models smdx_00213.xml).
# Everything not listed here stays at the static 0 the skeleton is
# initialised with.
OFFSET_CURRENT_TOTAL = 0             # A -- deliberately left NaN, not
                                      # summed or 0 -- see
                                      # build_static_context()'s comment
                                      # at this offset.
OFFSET_AC_POWER_TOTAL = 26           # W
OFFSET_TOTAL_WH_EXPORTED = 58        # TotWhExp
OFFSET_FREQUENCY = 24                # Hz -- single field, not per-phase

OFFSET_CURRENT_BY_PHASE = {1: 2, 2: 4, 3: 6}          # AphA, AphB, AphC
OFFSET_VOLTAGE_LN_BY_PHASE = {1: 10, 2: 12, 3: 14}    # PhVphA, PhVphB, PhVphC
OFFSET_AC_POWER_BY_PHASE = {1: 28, 2: 30, 3: 32}      # WphA, WphB, WphC

OFFSET_APPARENT_POWER_TOTAL = 34                      # VA
OFFSET_APPARENT_POWER_BY_PHASE = {1: 36, 2: 38, 3: 40}  # VAphA, VAphB, VAphC
OFFSET_REACTIVE_POWER_TOTAL = 42                      # VAR
OFFSET_REACTIVE_POWER_BY_PHASE = {1: 44, 2: 46, 3: 48}  # VARphA, VARphB, VARphC
OFFSET_POWER_FACTOR_TOTAL = 50                        # PF
OFFSET_POWER_FACTOR_BY_PHASE = {1: 52, 2: 54, 3: 56}  # PFphA, PFphB, PFphC


class Readings(NamedTuple):
    """One inverter's consistent snapshot of everything it contributes.
    Named fields make mismatches loud instead of silently swapping two
    similarly-typed floats -- this repo's sibling project hit exactly that
    bug with a plain positional tuple."""
    power_w: float
    energy_wh: float
    voltage_v: float
    current_a: float
    frequency_hz: float
    power_factor: float
    reactive_power_var: float


class PhaseTotals(NamedTuple):
    """One phase's combined contribution from every InverterSource wired
    to it -- see combine_phase()."""
    power_w: float
    current_a: float
    voltage_v: float
    reactive_power_var: float
    apparent_power_va: float
    power_factor: float


def combine_phase(readings: list) -> PhaseTotals:
    """Combines zero or more inverters' Readings that share one physical
    phase into that phase's totals.

    - power, current, reactive power: summed -- inverters on the same
      physical phase genuinely add (same as a single inverter's own
      current/power would, just from more than one source now).
    - voltage: averaged, not summed -- not an additive quantity. With the
      common case of one inverter per phase this is just that one
      inverter's own reading.
    - apparent power / power factor: *derived* from this phase's own P and
      Q (VA = hypot(P, Q), PF = P / VA), not averaged from each source's
      own reported PF -- this is the same derivation already verified
      end-to-end against real GEN24 hardware in the sibling
      hoymiles-fronius-meter project (VA and PF there round-tripped
      exactly consistent with a live pymodbus/mbpoll read), just applied
      to a phase's combined P/Q instead of a single inverter's.
    - a phase nobody is wired to gets an empty list here and comes back
      all zero, exactly like a genuinely unwired phase always has in the
      sibling project -- not a special case, just what falls out of
      summing/averaging nothing.
    """
    if not readings:
        return PhaseTotals(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    power = sum(r.power_w for r in readings)
    current = sum(r.current_a for r in readings)
    voltage = sum(r.voltage_v for r in readings) / len(readings)
    reactive = sum(r.reactive_power_var for r in readings)
    apparent = math.hypot(power, reactive)
    power_factor = power / apparent if apparent > 1e-6 else 0.0
    return PhaseTotals(power, current, voltage, reactive, apparent, power_factor)


def build_static_context(cfg: GlobalConfig) -> ModbusServerContext:
    """Builds the one combined meter's SunSpec Common(1) + Meter(213
    float) register skeleton. Manufacturer/model/serial/address are fixed
    at build time; every live register starts at 0 and is updated by
    update_registers() -- except OFFSET_CURRENT_TOTAL, set once here to
    SunSpec's "not implemented" sentinel and never touched again (see
    below)."""
    common_payload = (
        _ascii_to_registers(cfg.meter_manufacturer, 16) +
        _ascii_to_registers(cfg.meter_model, 16) +
        _ascii_to_registers("", 8) +            # Options
        _ascii_to_registers("", 8) +            # Version
        _ascii_to_registers(cfg.meter_serial, 16) +
        [cfg.meter_device_address]
    )
    assert len(common_payload) == 65

    meter_payload = [0] * 124
    # Total AC Current has no single physically meaningful value here --
    # separate phases are separate conductors, so summing per-phase
    # currents into one "total" isn't a real measured quantity the way
    # Total Power or Total VAR are (those combine correctly because
    # complex power *does* add linearly across phases at a shared node).
    # 0 would claim "measured, and it's zero", which is also wrong.
    # SunSpec's float representation defines NaN as the explicit "point
    # exists but has no value" sentinel for exactly this situation --
    # matches a real GEN24's own primary meter, which likewise reports no
    # value for its Total AC Current field.
    nan_hi, nan_lo = float_to_registers(float("nan"))
    meter_payload[OFFSET_CURRENT_TOTAL] = nan_hi
    meter_payload[OFFSET_CURRENT_TOTAL + 1] = nan_lo

    sparse = {
        40001: [0x5375, 0x6e53],   # "SunS" -- SunSpec marker
        40003: [1],                # Common model id
        40004: [65],               # Common model length
        40005: common_payload,
        40070: [213],              # Meter model id (213 = float, 3-phase)
        40071: [124],              # Meter model length
        40072: meter_payload,
        40196: [65535, 0],         # End-of-map marker
    }
    block = ModbusSparseDataBlock(sparse)
    slave_ctx = ModbusSlaveContext(di=block, co=block, ir=block, hr=block)
    return ModbusServerContext(slaves=slave_ctx, single=True)


def update_registers(context: ModbusServerContext, inverters: list, readings_by_source: list):
    """Combines every inverter's latest Readings (readings_by_source, in
    the same order as inverters) into the one meter's registers.

    readings_by_source[i] is the Readings for inverters[i] -- grouped here
    by meter_phase via combine_phase(), then written into that phase's
    registers, plus the grand totals across all phases.
    """
    slave_ctx = context[0x00] if hasattr(context, "__getitem__") else context
    # ModbusSlaveContext.setValues() adds +1 to the address itself (zero_mode
    # defaults to False), the same translation a real client's 0-based wire
    # address gets. So we pass "register-40072's-address-minus-one" here,
    # exactly as a client would, rather than the 1-based key directly.
    base = 40072 - 1

    def write(offset, value):
        hi, lo = float_to_registers(value)
        slave_ctx.setValues(3, base + offset, [hi, lo])

    by_phase = {1: [], 2: [], 3: []}
    for src, r in zip(inverters, readings_by_source):
        by_phase[src.meter_phase].append(r)
    phase_totals = {p: combine_phase(rs) for p, rs in by_phase.items()}

    total_power = sum(pt.power_w for pt in phase_totals.values())
    total_reactive = sum(pt.reactive_power_var for pt in phase_totals.values())
    # Total apparent power is the SUM of each phase's own already-correct
    # VA (sqrt(P^2+Q^2) computed per phase, in combine_phase()) -- not
    # sqrt(sum(P)^2 + sum(Q)^2) computed from the phase-summed totals.
    # Those two only agree when a single phase is active; with two+
    # phases active and opposite-signed reactive power (one phase
    # inductive, another capacitive), the latter lets them cancel in the
    # total, understating it -- physically wrong, since each phase's own
    # conductor still carries its own current based on its own P and Q,
    # regardless of another phase's character. This "arithmetic apparent
    # power" (sum of per-phase VA) is the standard way to define a
    # meaningful total apparent power for an unbalanced multi-phase
    # system for exactly this reason.
    total_apparent = sum(pt.apparent_power_va for pt in phase_totals.values())
    total_power_factor = total_power / total_apparent if total_apparent > 1e-6 else 0.0
    total_energy = sum(r.energy_wh for r in readings_by_source)
    # Frequency has no per-phase breakdown in this SunSpec model -- grid
    # frequency is physically identical across every phase anyway, so a
    # plain average across every reporting inverter (regardless of its
    # phase) is both simplest and correct.
    frequencies = [r.frequency_hz for r in readings_by_source]
    frequency = sum(frequencies) / len(frequencies) if frequencies else 0.0

    # SunSpec meter sign convention: positive W = import (grid -> load),
    # negative W = export/generation. Readings.power_w is always a
    # positive magnitude (OpenDTU just reports "how many watts is this
    # inverter making right now"), so both the total and each phase's
    # power must be negated -- otherwise generation would be reported as
    # if it were consumption. TotWhExp is unaffected: it's already an
    # unsigned "energy exported" accumulator, not a signed flow.
    # OFFSET_CURRENT_TOTAL is deliberately never written here -- it stays
    # at the NaN sentinel build_static_context() set it to once.
    write(OFFSET_AC_POWER_TOTAL, -total_power)
    write(OFFSET_TOTAL_WH_EXPORTED, total_energy)
    write(OFFSET_FREQUENCY, frequency)
    write(OFFSET_APPARENT_POWER_TOTAL, total_apparent)
    write(OFFSET_REACTIVE_POWER_TOTAL, total_reactive)
    write(OFFSET_POWER_FACTOR_TOTAL, total_power_factor)

    for phase, pt in phase_totals.items():
        write(OFFSET_AC_POWER_BY_PHASE[phase], -pt.power_w)
        write(OFFSET_VOLTAGE_LN_BY_PHASE[phase], pt.voltage_v)
        write(OFFSET_CURRENT_BY_PHASE[phase], pt.current_a)
        write(OFFSET_APPARENT_POWER_BY_PHASE[phase], pt.apparent_power_va)
        write(OFFSET_REACTIVE_POWER_BY_PHASE[phase], pt.reactive_power_var)
        write(OFFSET_POWER_FACTOR_BY_PHASE[phase], pt.power_factor)


# --------------------------------------------------------------------------
# MQTT aggregation
# --------------------------------------------------------------------------

class Aggregator:
    """Keeps the latest value seen on each of one inverter's configured MQTT
    topics and exposes thread-safe combined readings. One Aggregator per
    configured inverter (see main()) -- each only reacts to messages on its
    own topics, even though every inverter's aggregator shares the same
    underlying MQTT connection (see start_mqtt()).

    This is the object that bridges the two halves of the program: the MQTT
    callback thread (see start_mqtt()) calls on_message() every time a new
    value arrives, and the Modbus updater thread (see updater_loop()) calls
    totals() periodically to read out this inverter's combined values. A
    lock guards the seven dicts below because those two threads touch them
    concurrently -- without it we could read a torn/half-updated set of
    values.

    A topic with no message received yet simply contributes 0 to its
    combined value, so the meter starts up reporting 0 W / 0 A / 0 V rather
    than crashing or blocking startup on MQTT traffic that hasn't arrived.
    """

    def __init__(self, cfg: InverterSource):
        self.cfg = cfg
        self._lock = threading.Lock()
        # One (dict, patterns) pair per physical quantity we track. The
        # dict maps *actual received topic* -> latest value; patterns is
        # cfg's configured list, which may contain '+' wildcards (from
        # base-topic derivation) as well as plain literal topics.
        #
        # Literal (non-wildcard) topics are pre-seeded into the dict at
        # 0.0, so totals() always has a value even before the first MQTT
        # message. Wildcard patterns can't be pre-seeded this way (we don't
        # know the real topic that'll eventually match), so those start
        # absent and get inserted by on_message() the first time something
        # matches; totals()/_average() already treat an empty dict as 0.0,
        # so this is the same graceful "nothing received yet" behavior.
        self._power_values = self._seed(cfg.power_topic)
        self._energy_values = self._seed(cfg.energy_topic)
        self._voltage_values = self._seed(cfg.voltage_topic)
        self._current_values = self._seed(cfg.current_topic)
        self._frequency_values = self._seed(cfg.frequency_topic)
        self._power_factor_values = self._seed(cfg.power_factor_topic)
        self._reactive_power_values = self._seed(cfg.reactive_power_topic)

    @staticmethod
    def _seed(topics: list) -> dict:
        return {t: 0.0 for t in topics if "+" not in t and "#" not in t}

    def on_message(self, topic: str, payload: str):
        """Called from the MQTT client thread for every message on any
        subscribed topic (across every inverter, not just this one -- see
        start_mqtt()). Figures out whether the topic belongs to this
        inverter at all, and if so which quantity (power/energy/voltage/
        current/...) it's for, then stores the latest value -- we don't
        care about the history, only the most recent reading.

        A topic already present in a category's dict (the common case,
        every message after the first on a given topic) is a cheap
        membership check. The first message on a new wildcard-matched
        topic instead needs to check it against that category's
        configured patterns via _topic_matches(). A topic belonging to a
        different inverter entirely matches none of this inverter's
        patterns and is simply ignored.
        """
        try:
            value = float(payload)
        except ValueError:
            log.warning("Non-numeric payload on %s: %r", topic, payload)
            return
        with self._lock:
            for values, patterns in (
                (self._power_values, self.cfg.power_topic),
                (self._energy_values, self.cfg.energy_topic),
                (self._voltage_values, self.cfg.voltage_topic),
                (self._current_values, self.cfg.current_topic),
                (self._frequency_values, self.cfg.frequency_topic),
                (self._power_factor_values, self.cfg.power_factor_topic),
                (self._reactive_power_values, self.cfg.reactive_power_topic),
            ):
                if topic in values or any(_topic_matches(topic, p) for p in patterns):
                    values[topic] = value

    @staticmethod
    def _average(values: dict) -> float:
        return sum(values.values()) / len(values) if values else 0.0

    def totals(self) -> Readings:
        """Called from the Modbus updater thread. Combines every topic's
        latest value into this inverter's own Readings snapshot -- summed
        for power/current/reactive power, averaged for voltage/frequency/
        power factor, matching the usual "multiple topics feeding one
        quantity" convention (relevant if you ever configure more than one
        topic for the same field on this one inverter; combining *across*
        inverters happens separately, in combine_phase())."""
        with self._lock:
            power = sum(self._power_values.values())
            energy = sum(self._energy_values.values()) * self.cfg.energy_scale
            current = sum(self._current_values.values())
            reactive_power = sum(self._reactive_power_values.values())
            voltage = self._average(self._voltage_values)
            frequency = self._average(self._frequency_values)
            power_factor = self._average(self._power_factor_values)
        return Readings(
            power_w=power, energy_wh=energy, voltage_v=voltage, current_a=current,
            frequency_hz=frequency, power_factor=power_factor,
            reactive_power_var=reactive_power,
        )


def start_mqtt(global_cfg: GlobalConfig, sources: list) -> mqtt.Client:
    """One shared MQTT connection for every configured inverter.

    sources: list of (InverterSource, Aggregator) pairs. Subscribes to the
    union of every inverter's topics, and dispatches each incoming message
    to every inverter's Aggregator -- each Aggregator.on_message() already
    checks the topic against only its own configured patterns, so a
    message only ever updates the inverter(s) it actually belongs to, even
    though they all share one connection/subscription set.
    """
    client_id = "opendtu-fronius-meter-" + global_cfg.meter_serial
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    if global_cfg.mqtt_username:
        client.username_pw_set(global_cfg.mqtt_username, global_cfg.mqtt_password)

    def on_connect(client, userdata, connect_flags, reason_code, properties):
        if reason_code == 0:
            log.info("Connected to MQTT broker %s:%s as user %r",
                      global_cfg.mqtt_host, global_cfg.mqtt_port, global_cfg.mqtt_username)
            # Re-subscribing here (rather than once at startup) matters
            # because paho auto-reconnects after a dropped connection and
            # calls on_connect again -- subscriptions don't survive a
            # reconnect on their own, so this must run every time.
            all_topics = set()
            for src, _ in sources:
                all_topics.update(
                    src.power_topic + src.energy_topic + src.voltage_topic
                    + src.current_topic + src.frequency_topic
                    + src.power_factor_topic + src.reactive_power_topic
                )
            for t in sorted(all_topics):
                client.subscribe(t)
                log.info("Subscribed to %s", t)
        else:
            log.error("MQTT connect failed: %s", reason_code)

    def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
        log.warning("MQTT disconnected (%s) -- paho will auto-reconnect", reason_code)

    def on_message(client, userdata, msg):
        payload = msg.payload.decode(errors="replace")
        for _, aggregator in sources:
            aggregator.on_message(msg.topic, payload)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    client.connect(global_cfg.mqtt_host, global_cfg.mqtt_port, keepalive=60)
    client.loop_start()
    return client


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def updater_loop(global_cfg: GlobalConfig, context: ModbusServerContext, sources: list,
                  stop_event: threading.Event):
    """Runs in its own daemon thread for the lifetime of the process.

    This is deliberately the *only* thing that writes to the Modbus
    register context after startup. Each pass polls every inverter's
    Aggregator (cheap: just reads seven already-computed numbers behind a
    lock) and combines them into the one meter's registers, completely
    independent of both MQTT message timing and incoming Modbus requests.
    That decoupling is the whole point: a real Fronius GEN24 is intolerant
    of slow Modbus replies (see DOCS.md's Troubleshooting section), so the
    Modbus TCP server (driven by pymodbus's StartTcpServer in main()) must
    never block on MQTT I/O or any other slow operation -- it only ever
    reads whatever value this loop last wrote.

    sources: list of (InverterSource, Aggregator) pairs.
    """
    inverters = [src for src, _ in sources]
    while not stop_event.is_set():
        readings = [agg.totals() for _, agg in sources]
        update_registers(context, inverters, readings)
        log.debug(
            "Updated registers: %s",
            ", ".join(
                f"[{src.label()}] power={r.power_w:.1f}W energy={r.energy_wh:.1f}Wh "
                f"voltage={r.voltage_v:.1f}V current={r.current_a:.2f}A"
                for src, r in zip(inverters, readings)
            ),
        )
        # wait() both sleeps for update_interval seconds AND returns early if
        # stop_event gets set elsewhere -- this makes shutdown responsive
        # instead of waiting out a full sleep() first.
        stop_event.wait(global_cfg.update_interval)


def main():
    # GlobalConfig()/load_inverters() read every setting from environment
    # variables at call time (see the os.environ.get(...) defaults above,
    # and INVERTERS_JSON for the per-inverter list) -- there's no separate
    # "load config" step.
    global_cfg = GlobalConfig()
    inverters = load_inverters()
    validate_inverters(inverters)

    log.info("Configured %d inverter(s) feeding one combined meter", len(inverters))
    for src in inverters:
        log.info(
            "%s: power=%s energy=%s(x%.0f) voltage=%s current=%s freq=%s pf=%s var=%s",
            src.label(), src.power_topic, src.energy_topic, src.energy_scale,
            src.voltage_topic, src.current_topic, src.frequency_topic,
            src.power_factor_topic, src.reactive_power_topic,
        )
    log.info("Serving as '%s %s' (serial %s, device address %s) on %s:%s",
              global_cfg.meter_manufacturer, global_cfg.meter_model, global_cfg.meter_serial,
              global_cfg.meter_device_address, global_cfg.listen_host, global_cfg.listen_port)

    # Build the (mostly static) SunSpec register map once -- there's only
    # ever one, regardless of how many inverters feed it. Manufacturer/
    # model/serial/address and the register layout itself never change
    # after this -- only the handful of live registers written by
    # update_registers() move, and only via the updater thread below.
    context = build_static_context(global_cfg)
    sources = [(src, Aggregator(src)) for src in inverters]

    # The updater thread is what actually keeps the registers current; see
    # its own docstring for why it's decoupled from both MQTT and Modbus.
    # daemon=True means this thread won't keep the process alive on its own
    # -- it dies automatically when the main thread (blocked in
    # StartTcpServer below) exits.
    stop_event = threading.Event()
    updater = threading.Thread(
        target=updater_loop, args=(global_cfg, context, sources, stop_event), daemon=True
    )
    updater.start()

    # start_mqtt() connects and then returns immediately -- paho runs its
    # own network thread (client.loop_start()) that keeps calling each
    # aggregator's on_message() in the background for as long as the
    # process runs. Nothing here needs to wait on it.
    start_mqtt(global_cfg, sources)

    try:
        # This call blocks forever, serving Modbus TCP requests on the main
        # thread until the process is killed (e.g. Ctrl-C / docker stop).
        # Every request is answered purely from the register context built
        # above -- no MQTT or other I/O happens on this path.
        log.info("Starting Modbus TCP server...")
        StartTcpServer(context=context, address=(global_cfg.listen_host, global_cfg.listen_port))
    finally:
        # Only reached if StartTcpServer ever returns/raises (e.g. on
        # shutdown) -- tells the updater thread's wait() to return early
        # instead of running one more full update_interval cycle for nothing.
        stop_event.set()


if __name__ == "__main__":
    main()
