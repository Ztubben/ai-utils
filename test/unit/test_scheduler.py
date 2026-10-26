"""Unit tests for the shipped scheduler samples + install docs (US-012).

This is a docs/config story; its green gate is that the *shipped artifacts* exist
and stay well-formed:

  * a documented sample `.ralph.yml` (also asserted by test_validate_config),
  * sample scheduler units — a systemd .service + .timer and a cron entry — that
    run a tick (bin/ralph.sh) every 5 hours,
  * an install README covering submodule setup, config placement, schedule
    install, and the gh/Claude auth prerequisites.

These are drift-guards, mirroring how the checked-in agent prompts are tested:
if someone breaks the 5-hour cadence, drops an ExecStart, or removes the install
instructions, the gate goes red.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB_DIR = os.path.join(REPO_ROOT, "lib")
SCHEDULER = os.path.join(REPO_ROOT, "scheduler")
SAMPLE = os.path.join(REPO_ROOT, ".ralph.yml.sample")
README = os.path.join(REPO_ROOT, "README.md")
RALPH_SH = os.path.join(REPO_ROOT, "bin", "ralph.sh")

sys.path.insert(0, LIB_DIR)
import ralph_config  # noqa: E402


def _read(path):
    with open(path) as fh:
        return fh.read()


class SampleConfigShips(unittest.TestCase):
    def test_sample_config_is_shipped_and_validates(self):
        self.assertTrue(os.path.isfile(SAMPLE), "a documented sample .ralph.yml must ship")
        result = ralph_config.load_and_validate(SAMPLE)
        self.assertTrue(result.ok, result.errors)

    def test_sample_config_documents_the_concurrency_block(self):
        text = _read(SAMPLE)
        for key in ("concurrency:", "claim_ttl:", "feature_exclusive:", "max_slots:"):
            self.assertIn(key, text)


class SystemdSamples(unittest.TestCase):
    def setUp(self):
        self.service_path = os.path.join(SCHEDULER, "ralph.service")
        self.timer_path = os.path.join(SCHEDULER, "ralph.timer")
        self.assertTrue(os.path.isfile(self.service_path),
                        "a sample systemd service must ship under scheduler/")
        self.assertTrue(os.path.isfile(self.timer_path),
                        "a sample systemd timer must ship under scheduler/")
        self.service = _read(self.service_path)
        self.timer = _read(self.timer_path)

    def test_service_runs_a_tick_via_ralph_sh(self):
        self.assertIn("[Service]", self.service)
        self.assertIn("ExecStart", self.service)
        # The tick is bin/ralph.sh — the service must launch it.
        self.assertIn("ralph.sh", self.service)
        # A tick is a single run, not a long-lived daemon.
        self.assertIn("oneshot", self.service.lower())

    def test_timer_fires_every_five_hours(self):
        self.assertIn("[Timer]", self.timer)
        self.assertIn("[Install]", self.timer)
        # systemd every-5-hours cadence: OnCalendar=*-*-* 00/5:00:00
        self.assertIn("OnCalendar", self.timer)
        self.assertIn("00/5", self.timer,
                      "the timer must fire every 5 hours (OnCalendar 00/5)")
        self.assertIn("timers.target", self.timer)


class SystemdTemplateSamples(unittest.TestCase):
    """#143: a burst is one more template instance; the instance is the Slot."""

    def setUp(self):
        self.service_path = os.path.join(SCHEDULER, "ralph@.service")
        self.timer_path = os.path.join(SCHEDULER, "ralph@.timer")
        self.assertTrue(os.path.isfile(self.service_path),
                        "a systemd template service must ship under scheduler/")
        self.assertTrue(os.path.isfile(self.timer_path),
                        "a systemd template timer must ship under scheduler/")
        self.service = _read(self.service_path)
        self.timer = _read(self.timer_path)

    def test_template_service_runs_a_tick_as_its_instances_slot(self):
        self.assertIn("[Service]", self.service)
        self.assertIn("oneshot", self.service.lower())
        self.assertIn("ralph.sh", self.service)
        # The instance number is the Slot number.
        self.assertIn("Environment=RALPH_SLOT=%i", self.service)

    def test_template_timer_fires_its_instance_every_five_hours(self):
        self.assertIn("[Timer]", self.timer)
        self.assertIn("00/5", self.timer,
                      "the template timer must fire every 5 hours (OnCalendar 00/5)")
        self.assertIn("Unit=ralph@%i.service", self.timer)
        self.assertIn("timers.target", self.timer)


class SingleInstanceUnitsAreSlotOne(unittest.TestCase):
    """The non-template units and the cron line keep running as Slot 1: they
    name no Slot, and a Tick that is told none is Slot 1."""

    def test_no_single_instance_scheduler_names_a_slot(self):
        for name in ("ralph.service", "ralph.timer", "ralph.cron"):
            with self.subTest(name=name):
                text = _read(os.path.join(SCHEDULER, name))
                for line in text.splitlines():
                    if not line.lstrip().startswith("#"):
                        self.assertNotIn("RALPH_SLOT", line)

    def test_a_tick_told_no_slot_is_slot_one(self):
        self.assertIn(': "${RALPH_SLOT:=1}"', _read(RALPH_SH))


class CronSample(unittest.TestCase):
    def setUp(self):
        self.cron_path = os.path.join(SCHEDULER, "ralph.cron")
        self.assertTrue(os.path.isfile(self.cron_path),
                        "a sample cron entry must ship under scheduler/")
        self.cron = _read(self.cron_path)

    def test_cron_runs_a_tick_every_five_hours(self):
        # A crontab hour field of */5 = every 5 hours.
        self.assertIn("*/5", self.cron, "the cron entry must run every 5 hours")
        self.assertIn("ralph.sh", self.cron)


class RalphShIsExecutable(unittest.TestCase):
    def test_tick_script_is_executable(self):
        # The scheduler samples exec bin/ralph.sh directly, so it must be +x.
        self.assertTrue(os.path.isfile(RALPH_SH))
        self.assertTrue(os.access(RALPH_SH, os.X_OK), "bin/ralph.sh must be executable")


class InstallReadme(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(README), "an install README must ship")
        self.text = _read(README)
        self.low = self.text.lower()

    def test_covers_submodule_setup(self):
        self.assertIn("git submodule add", self.low)

    def test_covers_config_placement(self):
        # Copy the sample to the superproject root as .ralph.yml.
        self.assertIn(".ralph.yml.sample", self.text)
        self.assertIn(".ralph.yml", self.text)

    def test_covers_schedule_install_both_flavors(self):
        # The README must explain installing the schedule, covering both the
        # systemd timer and the cron entry shipped under scheduler/.
        self.assertIn("systemd", self.low)
        self.assertIn("cron", self.low)
        self.assertIn("scheduler/ralph.timer", self.text)
        self.assertIn("scheduler/ralph.cron", self.text)
        # And states the 5-hour cadence.
        self.assertIn("5 hours", self.low)

    def test_covers_slots_claims_and_bursts(self):
        # #143: Slots, Claims, the committed ceiling, a burst on and off, and
        # the Slot directory a first Tick leaves next to the checkout.
        for needle in ("Slot", "Claim", "concurrency.max_slots",
                       "scheduler/ralph@.service", "scheduler/ralph@.timer",
                       "systemctl --user enable --now ralph@2.timer",
                       "systemctl --user disable --now ralph@2.timer",
                       ".ralph-slot-"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.text)

    def test_covers_auth_prerequisites(self):
        # gh and Claude must both be authenticated for an unattended tick.
        self.assertIn("gh auth login", self.low)
        self.assertIn("claude", self.low)


if __name__ == "__main__":
    unittest.main()
