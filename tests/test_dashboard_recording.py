"""Dashboard regression tests for old reports, untrusted names and new counts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import dashboard


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, suffix, channels, *, id='nvr-01', completed='2026-10-26T14:00:00+07:00'):
        report = {'schema_version':2, 'nvr':{'id':id,'ip':'192.0.2.1'},
                  'check_completed_at':completed,
                  'device_info': {'model':'DS-8664NI-I8'},
                  'overall': {'status':'WARNING'},
                  'channel_status':{'online_count':1,'offline_count':1,'unknown_count':0},
                  'channel_inventory': {'channels':channels},
                  'storage': {'status':'CRITICAL','disks':[]}}
        path = self.root / f'{id}_{suffix}.json'
        path.write_text(json.dumps(report),encoding='utf-8')
        return path

    def test_new_report_summary_and_untrusted_text(self):
        dangerous = '<img src=x onerror=alert(1)>'
        self.write('new',[{'channel_id':'1','name':dangerous,'reported_status':'ONLINE',
                           'recording_result':{'status':'NO_RECENT_RECORDING_EVIDENCE','track_id':101}},
                          {'channel_id':'2','name':'Offline','reported_status':'OFFLINE',
                           'recording_result':{'status':'RECORDING','num_matches':1,'track_id':201}}])
        with patch.object(dashboard,'REPORTS_DIR',self.root):
            data = dashboard.read_dashboard_data()
        self.assertEqual(data['summary']['total_nvrs'],1)
        self.assertEqual(data['summary']['recording_evidence'],1)
        self.assertEqual(data['summary']['recording_no_evidence'],1)
        self.assertEqual(data['summary']['offline_with_evidence'],1)
        self.assertEqual(data['summary']['storage_critical'],1)
        self.assertEqual(data['nvrs'][0]['channels'][0]['name'],dangerous)
        self.assertNotIn('innerHTML', dashboard.HTML)
        self.assertIn('textContent', dashboard.HTML)

    def test_offline_historical_footage_counts_as_stored_recording(self):
        self.write('history',[{'channel_id':'1','reported_status':'OFFLINE',
                               'recording_result':{'status':'NO_RECENT_RECORDING_EVIDENCE'},
                               'historical_recording_result':{'status':'HISTORICAL_RECORDING_FOUND'}}])
        with patch.object(dashboard,'REPORTS_DIR',self.root):
            data = dashboard.read_dashboard_data()
        self.assertEqual(data['summary']['offline_with_evidence'],1)
        self.assertEqual(data['summary']['historical_evidence'],1)

    def test_old_reports_do_not_inherit_nvr_wide_recording_success(self):
        path = self.write('old',[{'channel_id':'1','reported_status':'ONLINE'}])
        report=json.loads(path.read_text())
        report['recording']={'status':'RECORDING_EVIDENCE_FOUND'}
        path.write_text(json.dumps(report))
        with patch.object(dashboard,'REPORTS_DIR',self.root):
            data=dashboard.read_dashboard_data()
        self.assertEqual(data['summary']['recording_evidence'],0)
        self.assertEqual(data['summary']['recording_unverified'],1)
        self.assertEqual(data['nvrs'][0]['channels'][0]['recording_result']['status'],'NOT_CHECKED')

    def test_invalid_json_is_skipped_and_newest_timestamp_wins(self):
        self.write('old',[{'channel_id':'1','name':'old'}],completed='2026-10-25T14:00:00+07:00')
        self.write('new',[{'channel_id':'1','name':'new'}],completed='2026-10-26T14:00:00+07:00')
        (self.root/'nvr-99_bad.json').write_text('{')
        with patch.object(dashboard,'REPORTS_DIR',self.root):
            data=dashboard.read_dashboard_data()
        self.assertEqual(data['report_errors'],1)
        self.assertEqual(data['nvrs'][0]['channels'][0]['name'],'new')

    def test_ubuntu_pilot_camera_recording_field_survives(self):
        from pathlib import Path
        data = dashboard._public_report({
            'nvr': {'id': 'nvr-01'},
            'channel_inventory': {'channels': [
                {'channel_id': '1', 'reported_status': 'ONLINE',
                 'camera_recording': {'status': 'RECORDING', 'track_id': 101, 'num_matches': 2}},
                {'channel_id': '2', 'reported_status': 'OFFLINE',
                 'camera_recording': {'status': 'SKIPPED_CAMERA_OFFLINE'}},
            ]},
            'camera_recording_summary': {'enabled': True, 'window_minutes': 60},
        }, Path('nvr-01_old.json'))
        self.assertEqual(data['channels'][0]['recording_result']['status'], 'RECORDING')
        self.assertEqual(data['channels'][0]['recording_result']['track_id'], 101)
        self.assertEqual(data['channels'][1]['recording_result']['status'], 'NOT_CHECKED')
        self.assertEqual(data['per_camera_recording']['window_minutes'], 60)

    def test_summary_does_not_relay_untrusted_fields(self):
        from pathlib import Path
        data = dashboard._public_report({
            'nvr': {'id': 'nvr-01'},
            'per_camera_recording': {'enabled': True, 'window_minutes': 60,
                                     'credential': 'do-not-relay'}
        }, Path('nvr-01.json'))
        self.assertNotIn('credential', data['per_camera_recording'])

    def test_old_pilot_offline_skip_normalizes_to_not_checked(self):
        value=dashboard._public_recording({'status':'SKIPPED_CAMERA_OFFLINE'})
        self.assertEqual(value['status'],'NOT_CHECKED')

if __name__=='__main__':
    unittest.main()

class IncompleteReportsTests(unittest.TestCase):
    def test_wrong_numeric_fields_do_not_crash_global_summary(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'nvr-01_bad.json').write_text(json.dumps({
                'nvr': {'id':'nvr-01'},
                'channel_status':{'online_count':'not a number','offline_count':None},
                'channel_inventory':{'channels':[None,{'channel_id':'1','reported_status':'UNKNOWN'}]},
                'storage':{'disks':'bad type'},
            }))
            with patch.object(dashboard,'REPORTS_DIR',root), patch.dict('os.environ',{'DASHBOARD_REFRESH_SECONDS':'oops'}):
                result=dashboard.read_dashboard_data()
            self.assertEqual(result['summary']['cameras_online'],0)
            self.assertEqual(result['summary']['recording_evidence'],0)
            self.assertEqual(result['refresh_seconds'],60)

class HistoricalDashboardTests(unittest.TestCase):
    def test_historical_status_is_independent_of_recent_state(self):
        from pathlib import Path
        report = dashboard._public_report({
            "nvr": {"id": "nvr-11"},
            "channel_inventory": {"channels": [{
                "channel_id": "12", "reported_status": "ONLINE",
                "recording_expectation": "continuous",
                "recording_result": {"status": "NO_RECENT_RECORDING_EVIDENCE"},
                "historical_recording_result": {
                    "status": "HISTORICAL_RECORDING_FOUND", "track_id": 1201,
                    "most_recent_segment_end": "2026-10-25T00:00:00+00:00",
                    "password": "private-data",
                    "intervals_examined": [{"start": "example", "secret": "private-data"}],
                },
            }]},
        }, Path("nvr-11_20261026.json"))
        row = report["channels"][0]
        self.assertEqual(row["recording_result"]["status"], "NO_RECENT_RECORDING_EVIDENCE")
        self.assertEqual(row["historical_recording_result"]["status"], "HISTORICAL_RECORDING_FOUND")
        self.assertNotIn("password", row["historical_recording_result"])
        self.assertNotIn("secret", row["historical_recording_result"]["intervals_examined"][0])

    def test_old_reports_have_no_false_historical_evidence(self):
        row = dashboard._public_report({
            "channel_inventory": {"channels": [{"channel_id": "1"}]}
        }, Path("nvr-01_old.json"))["channels"][0]
        self.assertEqual(row["historical_recording_result"]["status"], "HISTORICAL_NOT_CHECKED")
