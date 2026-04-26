
import unittest
from unittest.mock import MagicMock, patch, ANY
import sys
import os
from datetime import datetime

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from modules.log_monitor.log_monitor_service import LogMonitorSession
from modules.log_monitor.alert_engine import AlertRule

class TestLogMonitorAction(unittest.TestCase):
    def setUp(self):
        self.session = LogMonitorSession("test_session", "test_device", "com.test.pkg")
        
    def test_shell_action(self):
        # Create a rule with shell action
        rule = AlertRule(
            id="test_rule_shell",
            name="Test Shell Rule",
            type="keyword",
            pattern="test_error",
            severity="high",
            action="shell:echo test_cmd"
        )
        self.session.alert_engine.add_rule(rule)
        
        # Mock subprocess.run
        with patch('subprocess.run') as mock_run:
            # Trigger alert
            self.session.add_log("This is a test_error log")
            
            # Verify subprocess.run was called correctly
            mock_run.assert_called_with(
                ["adb", "-s", "test_device", "shell", "echo test_cmd"],
                timeout=5
            )
            
            # Verify alert record has action_taken
            alerts = self.session.alert_engine.get_alerts()
            self.assertTrue(len(alerts) > 0)
            self.assertEqual(alerts[0].action_taken, "Shell executed: echo test_cmd")

    def test_screenshot_action(self):
        # Create a rule with screenshot action
        rule = AlertRule(
            id="test_rule_screenshot",
            name="Test Screenshot Rule",
            type="keyword",
            pattern="test_crash",
            severity="high",
            action="screenshot"
        )
        self.session.alert_engine.add_rule(rule)
        
        # Mock subprocess.run
        with patch('subprocess.run') as mock_run:
            # Trigger alert
            self.session.add_log("This is a test_crash log")
            
            # Verify subprocess.run was called correctly
            # Note: stdout is a file object, so we use ANY
            mock_run.assert_called_with(
                ["adb", "-s", "test_device", "exec-out", "screencap", "-p"],
                stdout=ANY,
                timeout=10
            )
            
            # Verify alert record has action_taken
            alerts = self.session.alert_engine.get_alerts()
            self.assertTrue(len(alerts) > 0)
            self.assertTrue(alerts[0].action_taken.startswith("Screenshot saved:"))

if __name__ == '__main__':
    unittest.main()
