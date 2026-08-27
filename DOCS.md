# Documentation

## What this does

Fronius GEN24 inverters support registering **additional meters** beyond
the primary grid meter, once a primary meter exists. These can be tagged
as **Generator** (production) or **Consumer** (load), and feed into
Solar.web / Energy Profiling / the local power-flow display. The GEN24
doesn't verify the additional meter is genuine Fronius hardware -- it
just polls whatever SunSpec-compliant Modbus TCP device answers at the
configured IP:port. This App emulates exactly that: a SunSpec meter
(Common Model 1 + Meter Model 213, "float" representation) whose readings
are fed live from your OpenDTU inverters' MQTT data, combined into one
meter.

## Why one combined meter

A real Fronius GEN24's "add meter" dialog rejects a second Modbus TCP
meter entry that shares an IP address with an existing one -- regardless
of port. This was discovered while building this project's sibling,
[hoymiles-fronius-meter](https://github.com/ChrisDietrich/hoymiles-fronius-meter),
which serves one virtual meter per inverter: that works for exactly one
inverter, but a second entry at the same Home Assistant IP (even on a
different port) gets rejected by GEN24 itself with "Another Smart Meter
is configured with this address."

This App combines every configured inverter into **one** virtual meter
instead, so GEN24 only ever sees one Modbus TCP meter to register --
sidestepping the IP-uniqueness check entirely, with no need for a second
IP alias, a second network interface, or `host_network` access.

### Per-phase attribution within the combined meter

The SunSpec meter this App emulates (Model 213) is a 3-phase register
layout. Rather than collapsing every inverter's contribution onto a
single phase regardless of which leg it's actually wired to, each
inverter entry has its own **Wired phase** setting, and the combined
meter's L1/L2/L3 registers only reflect the inverters actually wired to
that phase:

- **Power, current, reactive power** on a phase are the *sum* of every
  inverter wired to it (multiple inverters can share a phase).
- **Voltage** on a phase is the *average* of every inverter wired to it
  (not additive).
- **Apparent power and power factor** on a phase are *derived* from that
  phase's own combined real/reactive power (`VA = sqrt(P^2 + Q^2)`,
  `PF = P / VA`) -- the same derivation already verified end-to-end
  against real GEN24 hardware in the sibling project, just applied per
  phase here instead of once for a single inverter.
- A phase with no inverters wired to it stays genuinely at 0, exactly
  like an unwired phase always has in the sibling project.
- The meter's **Total** power, energy, and reactive power are the sum
  across all three phases. **Total apparent power** is the *sum of each
  phase's own VA* (not `sqrt(sum(P)^2 + sum(Q)^2)` computed from the
  phase-summed totals) -- the latter would let opposite-signed reactive
  power on different phases (one inductive, one capacitive) misleadingly
  cancel out in the total, understating it; each phase's own conductor
  still carries its own current regardless of another phase's character.
  **Total power factor** is then `Total power / Total VA`.
- **Total AC Current is not reported** (reads as the SunSpec "not
  implemented" value, IEEE-754 NaN) rather than a sum or a 0 -- current
  on separate phases is current in separate conductors, with no single
  physically meaningful "total" the way power and reactive power have
  (those combine correctly because complex power genuinely adds linearly
  across phases at a shared node; current across *different* conductors
  doesn't). A real GEN24's own primary meter does the same for this
  field.

## Prerequisites

- A Fronius GEN24 with a primary meter already configured.
- One or more inverters managed by OpenDTU, publishing to MQTT.
- An MQTT broker reachable from both OpenDTU and this App.

## Configuration

The top-level settings (MQTT broker host/port/username/password, update
interval, log verbosity, Modbus TCP port, and the combined meter's
SunSpec identity) describe the one virtual meter this App serves, and
only need setting once. The **Inverters** list below them holds one
entry per OpenDTU-managed inverter to combine into that meter.

Home Assistant's Configuration UI doesn't show a description under each
individual field *inside* this list (a UI limitation, not something this
App's config can work around) -- the sections below are the full
reference for what each one does.

### The easy path (recommended)

For each inverter entry, fill in just:

- **OpenDTU base topic** -- the MQTT base topic that inverter's OpenDTU
  instance publishes under. Check OpenDTU's *Settings -> MQTT* page for
  "Topic". If you run one OpenDTU instance per inverter, each entry's
  base topic will be different.
- **Wired phase** -- see "Which phase is it wired to?" below.

Everything else in that entry derives automatically from its base topic,
using OpenDTU's standard topic layout:

- Power/energy come from `<base>/ac/power` and `<base>/ac/yieldtotal`
  (published once per OpenDTU instance, no inverter serial needed).
- Voltage/current/frequency/power factor/reactive power come from
  `<base>/<serial>/0/...`. You don't need to look up your inverter's
  serial number -- this App subscribes with an MQTT wildcard
  (`<base>/+/0/voltage` etc.), so the broker matches whatever serial is
  actually publishing under that base topic.

### Advanced: manual topic overrides

If an inverter's MQTT layout doesn't match OpenDTU's default (a custom
bridge, relabeled topics, etc.), leave that entry's **OpenDTU base topic**
empty and fill in its seven `*_topic` fields directly instead -- each
normally takes one topic, but also accepts a comma-separated list if you
need to sum multiple (e.g. two topics both feeding this entry's power),
except voltage/frequency/power factor, which are averaged across multiple
topics. Setting any of these explicitly overrides that field's
auto-derivation, even if a base topic is also set for that entry.

### Which phase is it wired to?

Set each entry's **Wired phase (L1/L2/L3)** to whichever phase that
inverter's AC output actually lands on at your electrical panel --
getting this right matters for per-phase accounting correctness, though
the meter's Total power/energy figures are unaffected either way. If you
don't know: leave it at L1 for now, then confirm later by watching your
primary meter's per-phase power in Home Assistant while briefly
disconnecting that inverter and observing which phase's reading drops.
Multiple inverters can share the same phase -- their contributions add.

### Modbus TCP port

Fronius GEN24's "add meter" dialog only offers a fixed choice of **502**
or **1502** -- not an arbitrary port. There's only one combined meter, so
only one port to set; pick whichever isn't already used by another
additional meter you register (e.g. a different App).

## Registering the meter in the GEN24

1. Log into the GEN24 web UI with Technician/service-level access.
2. Find the additional-meter configuration (Energy Profiling /
   "Zusatzzähler").
3. Add a new Modbus TCP meter: IP = your Home Assistant host's address,
   port = whatever you set above, role = **Generator**.
4. The Modbus address/unit ID GEN24 asks for doesn't need to match
   anything specific -- this App answers any unit ID it's asked.
5. Confirm the meter entry goes green in the GEN24 UI.

## Troubleshooting

- **Meter never goes green / GEN24 shows it as disconnected**: Fronius
  GEN24 units are known to discard slow Modbus responses (roughly a
  ~50ms cutoff) -- this App is architected specifically to avoid that
  (MQTT I/O never blocks the Modbus response path), so this is unlikely,
  but check network latency between the GEN24 and this App's host if it
  happens.
- **"Another Smart Meter is configured with this address"**: this is the
  exact problem this App exists to avoid -- make sure you're only
  registering **one** additional meter for this App (the combined one),
  not trying to register each inverter separately.
- **Values stuck at 0**: set **Log verbosity** to `debug` and check the
  log for the periodic `Updated registers: ...` line -- if a particular
  inverter's numbers never show non-zero values, check that MQTT
  credentials/base topic are correct and that OpenDTU is actually
  publishing (a quiet inverter at night will legitimately report ~0 W).
- **One phase's numbers look wrong**: check that inverter's **Wired
  phase** setting is correct -- see "Which phase is it wired to?" above.
