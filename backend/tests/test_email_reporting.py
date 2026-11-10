import os
import unittest
from unittest.mock import MagicMock,patch
import smtplib
from nvr_track.cli.run_all_nvr_reports import send_email_report


class EmailSafetyTests(unittest.TestCase):
    @patch.dict(os.environ, {'SMTP_HOST':'example.invalid','SMTP_PORT':'587','SMTP_SECURITY':'starttls',
                             'SMTP_USERNAME':'demo','SMTP_PASSWORD':'supersecret',
                             'SMTP_FROM':'demo@example.invalid','SMTP_TO':'test@example.invalid'})
    @patch('nvr_track.cli.run_all_nvr_reports.smtplib.SMTP')
    def test_smtp_exception_does_not_leak_credentials(self,smtp):
        fake=smtp.return_value.__enter__.return_value
        fake.send_message.side_effect=smtplib.SMTPException('password supersecret')
        sent,message=send_email_report([{'id':'nvr-01','overall':'HEALTHY','online':1}])
        self.assertFalse(sent)
        self.assertNotIn('supersecret',message)
        self.assertIn('SMTPException',message)

    @patch.dict(os.environ, {'SMTP_HOST':'','SMTP_USERNAME':'','SMTP_PASSWORD':'',
                             'SMTP_FROM':'','SMTP_TO':''})
    @patch('nvr_track.cli.run_all_nvr_reports.smtplib.SMTP')
    def test_no_recipient_does_not_attempt_email(self,smtp):
        sent,message=send_email_report([])
        self.assertFalse(sent)
        self.assertTrue(message.startswith('SKIPPED:'))
        smtp.assert_not_called()
