# Ralph does TDD off-target against a fakeable HAL

Ralph must write failing tests from a story's acceptance criteria before implementing (red → green). Because HIL stories exercise hardware that CI cannot observe, the expected architecture is a hardware abstraction layer (HAL): business logic is written against a HAL interface and tested on the host (CI) against a fake/mock HAL. Ralph TDDs the logic to green off-target; the real driver behind the HAL is what the human bench-verifies via the story's Bench Test Procedure.

For each acceptance criterion Ralph writes a host-runnable failing test against the HAL/fakes and implements to green. Where a HIL story genuinely has no host-testable seam, TDD covers whatever logic is host-testable and the bench procedure becomes the primary verification for the hardware-coupled remainder.

We rejected on-target/emulator testing in CI (Renode/QEMU/hardware runners) for v1: too much infrastructure for a shared submodule to assume, slow, and often unable to model analog/sensor reality anyway. The trade-off is that superprojects are expected to be HAL-structured so logic is fakeable on the host — a constraint on project architecture, but good hygiene regardless.
