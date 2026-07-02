"""Unit tests for the shipped scheduler samples + install docs (US-012).

This is a docs/config story; its green gate is that the *shipped artifacts* exist
and stay well-formed:

  * a documented sample `.ralph.yml` (also asserted by test_validate_config),
  * sample scheduler units — a systemd .service + .timer and a cron entry — that
    run a tick (bin/ralph.sh) every 3 hours,
  * an install README covering submodule setup, config placement, schedule
    install, and the gh/Claude auth prerequisites.

These are drift-guards, mirroring how the checked-in agent prompts are tested:
if someone breaks the 3-hour cadence, drops an ExecStart, or removes the install
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

    def test_timer_fires_every_three_hours(self):
        self.assertIn("[Timer]", self.timer)
        self.assertIn("[Install]", self.timer)
        # systemd every-3-hours cadence: OnCalendar=*-*-* 00/3:00:00
        self.assertIn("OnCalendar", self.timer)
        self.assertIn("00/3", self.timer,
                      "the timer must fire every 3 hours (OnCalendar 00/3)")
        self.assertIn("timers.target", self.timer)


class CronSample(unittest.TestCase):
    def setUp(self):
        self.cron_path = os.path.join(SCHEDULER, "ralph.cron")
        self.assertTrue(os.path.isfile(self.cron_path),
                        "a sample cron entry must ship under scheduler/")
        self.cron = _read(self.cron_path)

    def test_cron_runs_a_tick_every_three_hours(self):
        # A crontab hour field of */3 = every 3 hours.
        self.assertIn("*/3", self.cron, "the cron entry must run every 3 hours")
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
        # And states the 3-hour cadence.
        self.assertIn("3 hours", self.low)

    def test_covers_auth_prerequisites(self):
        # gh and Claude must both be authenticated for an unattended tick.
        self.assertIn("gh auth login", self.low)
        self.assertIn("claude", self.low)


if __name__ == "__main__":
    unittest.main()
