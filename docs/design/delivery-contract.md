# The delivery contract: three modes, three responsibilities

**Status:** Proposed (RFC), 2026-09-09. Most of it runs; one case and one
consequence do not, and they are named in *What holds today* at the end. It
becomes Accepted when the code respects all of it.
Discussion: [#232](https://github.com/never-dry/NeverDry/pull/232).

A zone's delivery mode is usually read as a setting: how NeverDry should decide
that a session is over. It is more than that. **The way a zone doses also
declares which witness it owns, and therefore which witness the system may
appeal to.** The user's declaration is a contract, and it binds the system
before it binds the user.

Reading it as a mere setting is what produced the field failure of 2026-09-08,
where a healthy valve was closed by a check that had no business running on that
zone at all, and the water it had already delivered was credited to nobody.

## The three declarations

### Case 1. A plain on/off valve (`estimated_flow`)

Declared: *I measure nothing. The duration is the dose.*

- A delivery sensor, if configured, serves exactly one purpose: refining the
  design flow rate with the observed one.
- It is **not binding on opening**. No condition on flow may prevent or
  interrupt a run.
- If the counter says nothing, the session proceeds. It had no part to play.

### Case 2. A metered valve (`flow_meter`)

Declared: *I measure the volume, and the dose ends when the volume is reached.*

- The meter **must** be declared. Without it the mode is meaningless and the
  configuration refuses it.
- The meter's **reporting cadence** is measured and kept.
- If the meter is to guard an opening, the guard's threshold **is that measured
  cadence**, not a NeverDry constant. This is the centre of the whole document:
  the threshold belongs to the device.
- A meter whose cadence is not yet known cannot guard. Not out of caution, but
  because the quantity that defines the threshold does not exist yet.

### Case 3. A smart valve dosing by volume (`volume_preset`)

Declared: *I arm the dose on the valve, and the valve closes itself.*

- The dose never depends on flow. The valve governs it.
- A declared meter falls under Case 2: it may guard, by the same rule, with the
  window derived from that meter's cadence.

## The invariant: who answers for the water

| Mode | Answers for delivery | Consequence |
|---|---|---|
| `estimated_flow` | **the user** | declared the rate, so the deficit falls by force of that declaration |
| `flow_meter` | **NeverDry** | measures, so answers for the measurement and for its timing |
| `volume_preset` | **the valve** | accepted the dose and closes itself, so answers for it |

This is what turns three conventions into one rule. It also settles a question
that looked like a policy choice and is not.

## The deficit always falls, with three different titles

| Mode | Verifiable? | Deficit falls? | By force of |
|---|---|---|---|
| `estimated_flow` | never, **by contract** | **yes** | the declared design rate |
| `flow_meter` | **yes**, once the meter's cadence is allowed for | **yes** | the measurement, awaited |
| `volume_preset` | verifiable by the device itself | **yes** | the valve's own declaration |

There is no configuration in which the correct answer is to credit nothing. Only
the *title* changes.

The question that sounded reasonable, "should we trust an unverifiable
delivery?", was malformed. In Case 1 unverifiability is not a fault, it is the
user's declared choice, and denying its consequences means refusing a
configuration after having accepted it. In Case 2 unverifiability does not exist,
provided the window is the right one. In Case 3 the verification happens, and the
valve performs it.

Two consequences follow, and both were field defects before they were rules:

- **A flow failure cannot prevent crediting.** It may qualify a delivery, never
  cancel it. On 2026-09-08 a zone delivered six measured litres and its deficit
  moved by 0.1 mm, because the session ended in error before the water was
  counted. So the water left the pipe and the model did not know.
- **In Case 2 the count waits for the meter's last word.** A metered session is
  not over when the valve shuts, it is over when the counter has finished
  speaking. Counting at the close understated one measured session by 5%: 271
  litres against roughly 285 delivered, because the closing tick landed three
  and a half minutes later.

## Why a guard in Case 1 is somebody else's control

The objection to the guard in `estimated_flow` is often put as "a second check
cannot hurt". It can, and the reason is not redundancy.

In Case 1 the responsible party is the user. A flow guard makes NeverDry assume a
responsibility that is not its own, that of verifying delivery, and then **refuse
to carry out what the user declared** in the name of that verification. It is not
abusive because it is a second check. It is abusive because it is somebody
else's check.

There is a plainer test of the same thing: if a still meter could still close the
valve, then declaring a plain valve and declaring a metered one would produce
identical behaviour on opening, and the choice the user made would mean nothing.

## The obligation that comes with the guard

If NeverDry answers for the measurement in Case 2, the guard has a right to
exist. It also carries a symmetric obligation: whoever assumes a responsibility
must answer for it when they get it wrong.

When a 90 s window expired on a meter publishing every 300 s, the verification
failed through an error of ours, in choosing the threshold. It was recorded as:

```
ACTUATION_FAILED     ->     "the valve failed to actuate"
```

which attributes it to the device. That is not only unfair, it is expensive: the
log accused a valve that had done its job, and the diagnosis took a day. So when
our own window cannot conclude, the outcome is `FLOW_UNVERIFIABLE` and the run
proceeds. A failure kind that names the device is reserved for evidence about the
device.

## Where the title is recorded

`DeliveryQuality` (`driver.py`) already distinguishes `MEASURED`, `ESTIMATED`,
`PARTIAL`, `DELAYED`, `LOW_CONFIDENCE` and `DECLARED`. It came out of an external
review ([#74](https://github.com/never-dry/NeverDry/issues/74)) and is written on
every `DeliveryResult`. Nothing reads it: not the controller, not the sensors,
not the card.

It stayed inert because the decision that would consume it had not been taken.
While the question was "credit or not", quality was useless, since a binary
session outcome answered it. With the rule above, quality finds its consumer:
**it does not decide whether to credit, it records the title under which
crediting happened.** The three titles in the table are three values the enum
already has.

It is also how the difference between the three cases becomes visible without
being studied. A session that reduces the deficit declaring `estimated` is saying
"I trusted your figure"; one declaring `measured` is saying "I counted it".

## What holds today

| | State |
|---|---|
| Case 1 is not bound by flow | **implemented**: the guard arms only outside `estimated_flow`, and the meter keeps feeding the learned rate |
| Case 2 threshold is the device's cadence | **implemented**: no cadence means no guard, and a cadence too long to guard yields a verdict rather than a longer window |
| Case 2 count waits for the meter | **implemented**: the post-close wait derives from the same cadence |
| A refusal of ours does not cancel the credit | **implemented for cumulative meters**: the deficit is credited from the settled reading. Rate-only sensors have no cumulative baseline and are not covered yet |
| Case 3 may guard, by the Case 2 rule | **not implemented**: `_deliver_volume_preset` bypasses the driver deliberately, so the arming that exists there is unreachable |
| `DeliveryQuality` records the title | **not implemented**: still written and never read |

The gap in Case 3 is worth stating plainly, because it is easy to mistake for
compliance. That mode is today the only one immune to the guard defect, but by
accident rather than by design: it never reaches the state machine. Under this
contract it should move from "no guard, by accident" to "a guard available by
choice, with the right rule", and it is the case where a guard is most useful,
since the dose is run by a device whose cycle NeverDry does not control.

## Open

- Whether Case 3 should be routed through the operator, or the unreachable
  arming removed so the door is not left ajar.
- What honest figure, if any, can be credited on a rate-only sensor after a
  refusal, given there is no cumulative baseline to difference.
- Whether the deferred credit should also move the session counters (total and
  yearly water), which today stay with the paths that completed a session. The
  water did leave the pipe; the session did not complete.
