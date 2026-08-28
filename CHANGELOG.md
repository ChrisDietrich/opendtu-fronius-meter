# Changelog

## 0.1.1

- Add optional **Inverter name** field per entry -- purely cosmetic (never
  sent to MQTT/Modbus/GEN24), just makes entries easier to tell apart in
  the Configuration UI and in logs. Falls back to the OpenDTU base topic
  when left blank, same as before this field existed.
- Add debug-level logging for troubleshooting: each incoming MQTT value
  (which inverter/quantity/topic it fed), each inverter's aggregated
  totals before combination, and a new "Served registers" line showing
  the actual per-phase and total values written to Modbus -- previously
  only a partial pre-combination summary was logged, with no visibility
  into what was actually served.

## 0.1.0

- Initial release: emulates one SunSpec Common Model 1 + Meter Model 213
  (float) Modbus TCP meter, fed live from any number of OpenDTU-managed
  inverters' MQTT data and combined into that one meter.
- Each inverter is attributed to its own wired phase (L1/L2/L3) within
  the combined meter's 3-phase register set; multiple inverters may share
  a phase (their contributions add). A phase with no inverters wired to
  it stays genuinely at 0.
- Total apparent power is the sum of each phase's own VA, not a vector
  recombination of phase-summed real/reactive power (the latter can let
  opposite-signed reactive power on different phases misleadingly cancel
  in the total). Total AC Current is reported as SunSpec's "not
  implemented" NaN sentinel rather than a sum or a 0, matching a real
  GEN24's own primary meter -- current on separate phase conductors has
  no single physically meaningful total the way power does.
- Exists because a real Fronius GEN24 rejects a second Modbus TCP
  "additional meter" entry sharing an IP with an existing one, regardless
  of port -- see this project's sibling,
  [hoymiles-fronius-meter](https://github.com/ChrisDietrich/hoymiles-fronius-meter),
  which hit exactly that registering a second inverter.
