"""Offline-only tests; never communicate with an NVR."""
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET

from nvr_track.nvr.camera_recording import (
    check_all_cameras, get_tracks, parse_tracks, resolve_track, simplify_search,
)
from nvr_track.nvr.recording import check_recording_evidence, parse_search_timestamp


class TrackTests(TestCase):
    TRACKS = b'<TrackList><Track><id>101</id><srcChannel>1</srcChannel></Track>' \
             b'<Track><id>102</id><srcChannel>1</srcChannel></Track>' \
             b'<Track><id>201</id><srcChannel>2</srcChannel></Track>' \
             b'<Track><id>301</id></Track></TrackList>'

    def test_track_mapping_is_verified_per_channel(self):
        tracks = parse_tracks(self.TRACKS)
        self.assertEqual(resolve_track('1', tracks)[:2], ('VERIFIED', 101))
        self.assertEqual(resolve_track('2', tracks)[:2], ('VERIFIED', 201))
        self.assertEqual(resolve_track('3', tracks)[:2], ('VERIFIED', 301))
        self.assertEqual(resolve_track('4', tracks)[0], 'TRACK_NOT_FOUND')

    def test_wrong_src_channel_is_not_accepted_as_candidate(self):
        self.assertEqual(resolve_track('1', {101: '2'})[0], 'TRACK_MAPPING_UNVERIFIED')
        self.assertEqual(resolve_track('1', {101: None})[0], 'VERIFIED')
        self.assertEqual(resolve_track('not-id', {101: None})[0], 'TRACK_MAPPING_UNVERIFIED')

    def test_multiple_main_tracks_are_ambiguous(self):
        self.assertEqual(resolve_track('1', {101: '1', 201: '1'})[0], 'TRACK_MAPPING_UNVERIFIED')

    def test_xml_parse_rejects_empty_unexpected_or_duplicate_ids(self):
        for content in (b'<Error><code>1</code></Error>', b'<TrackList/>',
                        b'<TrackList><Track><id>101</id></Track><Track><id>101</id></Track></TrackList>'):
            with self.assertRaises(ValueError):
                parse_tracks(content)

    @patch('nvr_track.nvr.camera_recording.requests.Session')
    def test_permission_problem_not_assumed_missing_track(self, session_factory):
        session = session_factory.return_value.__enter__.return_value
        session.get.return_value.status_code = 403
        result = get_tracks('192.0.2.1', 80, 'fake', 'fake')
        self.assertEqual(result['status'], 'PERMISSION_DENIED')
        self.assertEqual(result['tracks'], {})
        self.assertEqual(session.get.call_count, 1)
        self.assertFalse(session.post.called)

    @patch('nvr_track.nvr.camera_recording.requests.Session')
    def test_connection_problem_not_assumed_missing_track(self, session_factory):
        from requests.exceptions import Timeout
        session_factory.return_value.__enter__.return_value.get.side_effect = Timeout()
        result = get_tracks('192.0.2.1', 80, 'fake', 'fake')
        self.assertEqual(result['status'], 'CONNECTION_ERROR')


class RecordingClassificationTests(TestCase):
    def test_explicit_status_and_most_recent_end(self):
        raw = {'status': 'RECORDING_EVIDENCE_FOUND', 'message': 'OK', 'num_matches': 2,
               'matches': [{'track_id': 101, 'start_utc': '2026-10-26T01:00:00+00:00',
                            'end_utc': '2026-10-26T01:15:00+00:00'},
                           {'track_id': 101, 'start_utc': '2026-10-26T01:16:00+00:00',
                            'end_utc': '2026-10-26T01:35:00+00:00'}]}
        r = simplify_search(raw, 101, 60)
        self.assertEqual(r['status'], 'RECORDING')
        self.assertEqual(r['most_recent_segment_end'], '2026-10-26T01:35:00+00:00')
        self.assertTrue(r['track_verified'])

    def test_no_matches_is_not_never_recorded(self):
        r = simplify_search({'status': 'NO_MATCHES_IN_WINDOW'}, 201, 60)
        self.assertEqual(r['status'], 'NO_RECENT_RECORDING_EVIDENCE')
        self.assertIn('not proof', r['message'])
        self.assertIsNone(r['most_recent_segment_end'])

    def test_false_positive_evidence_without_verified_segment_is_inconclusive(self):
        raw = {'status': 'RECORDING_EVIDENCE_FOUND',
               'message': 'Device says yes',
               'requested_window_utc': {'start':'2026-10-26T05:00:00+00:00',
                                        'end':'2026-10-26T06:00:00+00:00'},
               'matches': [{'track_id':201, 'start_utc':'2026-10-26T05:10:00+00:00',
                            'end_utc':'2026-10-26T05:20:00+00:00'}]}
        result = simplify_search(raw, 101, 60)
        self.assertEqual(result['status'], 'CHECK_INCONCLUSIVE')
        self.assertIsNone(result['num_matches'])

    def test_anomaly_and_network_search_error_distinction(self):
        self.assertEqual(simplify_search({
            'status': 'RECORDING_EVIDENCE_FOUND_WITH_TIME_ANOMALY',
            'matches': [{'track_id': 101, 'start_utc': '2026-10-26T01:00:00+00:00',
                         'end_utc': '2026-10-26T01:15:00+00:00'}]},
            101, 60)['status'], 'RECORDING_EVIDENCE_WITH_ANOMALY')
        self.assertEqual(simplify_search({'status': 'PERMISSION_DENIED'}, 101, 60)['status'], 'CHECK_ERROR')
        self.assertEqual(simplify_search({'status': 'INCONSISTENT_OR_UNEXPECTED_RESPONSE'},
                                         101, 60)['status'], 'CHECK_INCONCLUSIVE')

    def test_timezone_local_z_semantics(self):
        local_tz = timezone(timedelta(hours=7))
        result = parse_search_timestamp('2026-10-26T15:00:00Z', local_tz)
        self.assertEqual(result.isoformat(), '2026-10-26T08:00:00+00:00')

    @patch('nvr_track.nvr.recording.requests.Session')
    def test_overlapping_segment_starting_before_window_is_evidence(self, factory):
        fake_session = factory.return_value.__enter__.return_value
        fake_session.post.return_value.status_code = 200
        fake_session.post.return_value.content = b'''<CMSearchResult>
          <responseStatus>true</responseStatus><responseStatusStrg>OK</responseStatusStrg>
          <numOfMatches>1</numOfMatches><matchList><searchMatchItem>
          <trackID>101</trackID><timeSpan><startTime>2026-10-26T14:59:00Z</startTime>
          <endTime>2026-10-26T15:30:00Z</endTime></timeSpan>
          </searchMatchItem></matchList></CMSearchResult>'''
        result = check_recording_evidence('192.0.2.1','x','y',track_id=101,
            clock_snapshot=(datetime(2026,10,26,9,0,tzinfo=timezone.utc),
                            timezone(timedelta(hours=7))))
        self.assertEqual(result['status'], 'RECORDING_EVIDENCE_FOUND')
        self.assertEqual(result['num_matches'], 1)
        fake_session.get.assert_not_called()  # shared NVR clock, no redundant GET
        fake_session.post.assert_called_once()


class CameraBatchTests(TestCase):
    def setUp(self):
        self.channels = [{'channel_id': '1', 'reported_status': 'ONLINE'},
                         {'channel_id': '2', 'reported_status': 'OFFLINE'},
                         {'channel_id': '3', 'reported_status': 'ONLINE'}]

    @patch('nvr_track.nvr.camera_recording.get_clock')
    @patch('nvr_track.nvr.camera_recording.get_tracks')
    def test_offline_checked_and_no_evidence_counted(self, tracks, clock):
        tracks.return_value = {'status': 'VERIFIED', 'tracks': {101: '1', 201: '2', 301: '3'}}
        clock.return_value = datetime(2026,10,26,9,tzinfo=timezone.utc), timezone(timedelta(hours=7))
        visited = []
        def search(**kwargs):
            visited.append(kwargs['track_id'])
            return {'status': 'NO_MATCHES_IN_WINDOW' if kwargs['track_id'] == 301
                    else 'RECORDING_EVIDENCE_FOUND',
                    'matches': [{'track_id':kwargs['track_id'], 'start_utc':'2026-10-26T08:00:00+00:00',
                                 'end_utc':'2026-10-26T09:00:00+00:00'}] if kwargs['track_id'] != 301 else [],
                    'num_matches': 0 if kwargs['track_id'] == 301 else 1}
        summary = check_all_cameras(ip='192.0.2.1',port=80,username='a',password='b',
                                    channels=self.channels,enabled=True,search=search)
        self.assertEqual(visited,[101,201,301])
        self.assertEqual(summary['checked'],3)
        self.assertEqual(summary['evidence'],2)
        self.assertEqual(summary['no_recent_evidence'],1)
        self.assertEqual(summary['offline_with_evidence'],1)
        self.assertEqual(summary['offline_without_recent_evidence'],0)
        self.assertEqual(self.channels[1]['reported_status'], 'OFFLINE')
        self.assertEqual(self.channels[1]['recording_result']['status'], 'RECORDING')

    @patch('nvr_track.nvr.camera_recording.get_tracks')
    def test_unverified_track_list_never_searches(self, tracks):
        tracks.return_value={'status':'PERMISSION_DENIED','tracks':{},'message':'Not authorized'}
        search=MagicMock()
        summary=check_all_cameras(ip='192.0.2.1',port=80,username='a',password='b',
                                  channels=self.channels,enabled=True,search=search)
        search.assert_not_called()
        self.assertEqual(summary['track_mapping_problems'],3)
        self.assertEqual(summary['checked'],0)

    @patch('nvr_track.nvr.camera_recording.get_tracks')
    def test_disabled_is_not_reported_as_recording(self, tracks):
        summary=check_all_cameras(ip='192.0.2.1',port=80,username='a',password='b',
                                  channels=self.channels,enabled=False)
        tracks.assert_not_called()
        self.assertEqual(summary['not_checked'],3)
        self.assertEqual(summary['evidence'],0)
        self.assertEqual(self.channels[0]['recording_result']['status'], 'NOT_CONFIGURED')

    @patch('nvr_track.nvr.camera_recording.get_clock')
    @patch('nvr_track.nvr.camera_recording.get_tracks')
    def test_clock_failure_inconclusive_for_verified_tracks(self, tracks, clock):
        tracks.return_value={'status':'VERIFIED','tracks':{101:None,201:None,301:None}}
        clock.return_value=None
        search=MagicMock()
        summary=check_all_cameras(ip='192.0.2.1',port=80,username='a',password='b',
                                  channels=self.channels,enabled=True,search=search,
                                  sleep=lambda seconds: None)
        self.assertEqual(clock.call_count, 2)   # one retry for an unreachable clock
        search.assert_not_called()
        self.assertEqual(summary['inconclusive'],3)
        self.assertEqual(summary['evidence'],0)


class ConnectionRetryTests(TestCase):
    """A brief network drop is retried once; other failures are not."""

    def setUp(self):
        self.channels = [{'channel_id': '1', 'reported_status': 'ONLINE'},
                         {'channel_id': '2', 'reported_status': 'ONLINE'}]
        self.clock = (datetime(2026, 10, 26, 9, tzinfo=timezone.utc), timezone(timedelta(hours=7)))
        self.found = lambda tid: {
            'status': 'RECORDING_EVIDENCE_FOUND', 'num_matches': 1,
            'matches': [{'track_id': tid, 'start_utc': '2026-10-26T08:30:00+00:00',
                         'end_utc': '2026-10-26T08:59:00+00:00'}]}

    def run_batch(self, search, delay=15, **extra):
        self.sleeps = []
        return check_all_cameras(ip='192.0.2.1', port=80, username='a', password='b',
                                 channels=self.channels, enabled=True, search=search,
                                 retry_delay_seconds=delay, sleep=self.sleeps.append, **extra)

    @patch('nvr_track.nvr.camera_recording.get_clock')
    @patch('nvr_track.nvr.camera_recording.get_tracks')
    def test_connection_error_is_retried_once_and_can_recover(self, tracks, clock):
        tracks.return_value = {'status': 'VERIFIED', 'tracks': {101: '1', 201: '2'}}
        clock.return_value = self.clock
        calls = []
        def search(**kw):
            calls.append(kw['track_id'])
            if kw['track_id'] == 101 and calls.count(101) == 1:
                return {'status': 'CONNECTION_OR_REQUEST_ERROR', 'message': 'timed out'}
            return self.found(kw['track_id'])
        summary = self.run_batch(search)
        self.assertEqual(calls, [101, 101, 201])
        self.assertEqual(self.sleeps, [15])
        self.assertEqual(summary['evidence'], 2)
        self.assertTrue(self.channels[0]['recording_result']['retried_after_connection_error'])
        self.assertNotIn('retried_after_connection_error', self.channels[1]['recording_result'])

    @patch('nvr_track.nvr.camera_recording.get_clock')
    @patch('nvr_track.nvr.camera_recording.get_tracks')
    def test_persistent_connection_error_stays_check_error(self, tracks, clock):
        tracks.return_value = {'status': 'VERIFIED', 'tracks': {101: '1', 201: '2'}}
        clock.return_value = self.clock
        calls = []
        def search(**kw):
            calls.append(kw['track_id'])
            return {'status': 'CONNECTION_OR_REQUEST_ERROR', 'message': 'timed out'}
        summary = self.run_batch(search)
        self.assertEqual(calls, [101, 101, 201, 201])   # one retry per camera, never more
        self.assertEqual(summary['search_errors'], 2)
        self.assertEqual(summary['evidence'], 0)

    @patch('nvr_track.nvr.camera_recording.get_clock')
    @patch('nvr_track.nvr.camera_recording.get_tracks')
    def test_no_matches_is_not_retried_and_invalid_reply_once(self, tracks, clock):
        tracks.return_value = {'status': 'VERIFIED', 'tracks': {101: '1', 201: '2'}}
        clock.return_value = self.clock
        calls = []
        def search(**kw):
            calls.append(kw['track_id'])
            return ({'status': 'NO_MATCHES_IN_WINDOW', 'matches': [], 'num_matches': 0}
                    if kw['track_id'] == 101 else
                    {'status': 'INCONSISTENT_OR_UNEXPECTED_RESPONSE', 'message': 'bad'})
        summary = self.run_batch(search)
        self.assertEqual(calls, [101, 201, 201])   # invalid reply: one more try only
        self.assertEqual(self.sleeps, [3])
        self.assertEqual(summary['inconclusive'], 1)
        self.assertTrue(self.channels[1]['recording_result']['retried_after_invalid_reply'])

    @patch('nvr_track.nvr.camera_recording.get_clock')
    @patch('nvr_track.nvr.camera_recording.get_tracks')
    def test_invalid_reply_that_validates_on_retry_counts_as_evidence(self, tracks, clock):
        tracks.return_value = {'status': 'VERIFIED', 'tracks': {101: '1', 201: '2'}}
        clock.return_value = self.clock
        replies = {101: [self.found(101)],
                   201: [{'status': 'INCONSISTENT_OR_UNEXPECTED_RESPONSE'}, self.found(201)]}
        summary = self.run_batch(lambda **kw: replies[kw['track_id']].pop(0))
        self.assertEqual(summary['evidence'], 2)
        self.assertEqual(summary['inconclusive'], 0)

    @patch('nvr_track.nvr.camera_recording.get_clock')
    @patch('nvr_track.nvr.camera_recording.get_tracks')
    def test_zero_delay_also_disables_invalid_reply_retry(self, tracks, clock):
        tracks.return_value = {'status': 'VERIFIED', 'tracks': {101: '1', 201: '2'}}
        clock.return_value = self.clock
        calls = []
        def search(**kw):
            calls.append(kw['track_id'])
            return {'status': 'INCONSISTENT_OR_UNEXPECTED_RESPONSE'}
        self.run_batch(search, delay=0)
        self.assertEqual(calls, [101, 201])

    @patch('nvr_track.nvr.camera_recording.get_clock')
    @patch('nvr_track.nvr.camera_recording.get_tracks')
    def test_zero_delay_disables_retry(self, tracks, clock):
        tracks.return_value = {'status': 'VERIFIED', 'tracks': {101: '1', 201: '2'}}
        clock.return_value = self.clock
        calls = []
        def search(**kw):
            calls.append(kw['track_id'])
            return {'status': 'CONNECTION_OR_REQUEST_ERROR', 'message': 'timed out'}
        self.run_batch(search, delay=0)
        self.assertEqual(calls, [101, 201])
        self.assertEqual(self.sleeps, [])

    @patch('nvr_track.nvr.camera_recording.get_clock')
    @patch('nvr_track.nvr.camera_recording.get_tracks')
    def test_track_list_and_clock_connection_failures_are_retried(self, tracks, clock):
        tracks.side_effect = [{'status': 'CONNECTION_ERROR', 'tracks': {}, 'message': 'timed out'},
                              {'status': 'VERIFIED', 'tracks': {101: '1', 201: '2'}}]
        clock.side_effect = [None, self.clock]
        summary = self.run_batch(lambda **kw: self.found(kw['track_id']))
        self.assertEqual(tracks.call_count, 2)
        self.assertEqual(clock.call_count, 2)
        self.assertEqual(self.sleeps, [15, 15])
        self.assertEqual(summary['evidence'], 2)
        self.assertEqual(summary['track_mapping_problems'], 0)

    @patch('nvr_track.nvr.camera_recording.get_tracks')
    def test_track_list_permission_problem_is_not_retried(self, tracks):
        tracks.return_value = {'status': 'PERMISSION_DENIED', 'tracks': {}, 'message': 'no'}
        summary = self.run_batch(MagicMock())
        self.assertEqual(tracks.call_count, 1)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(summary['track_mapping_problems'], 2)

    @patch('nvr_track.nvr.camera_recording.get_clock')
    @patch('nvr_track.nvr.camera_recording.get_tracks')
    def test_retry_never_exceeds_the_time_budget(self, tracks, clock):
        tracks.return_value = {'status': 'CONNECTION_ERROR', 'tracks': {}, 'message': 'x'}
        summary = self.run_batch(MagicMock(), delay=20, budget_seconds=15)
        self.assertEqual(tracks.call_count, 1)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(summary['track_mapping_problems'], 2)
