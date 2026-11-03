"""No NVR requests in tests; validate calendar evidence and safe rollout."""
from datetime import date, datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch, MagicMock

from app.retention import (
    parse_month, query_month, scan_calendar, cached_result, deferred_result,
    VERIFIED, PARTIAL, PENDING, STALE, UNSUPPORTED, INCONCLUSIVE, ERROR,
)
from app.camera_recording import check_all_cameras
from app.dashboard import _public_retention


TZ = timezone(timedelta(hours=7))
NOW = datetime(2026, 10, 28, 8, 0, tzinfo=TZ)


def month_xml(days):
    entries = "".join(f"<day><id>{n}</id><dayOfMonth>{n}</dayOfMonth>"
                      f"<record>{str(v).lower()}</record><recordType>time</recordType></day>"
                      for n, v in days.items())
    return (f'<trackDailyDistribution xmlns="http://www.isapi.com/ver20/XMLSchema">'
            f'<dayList>{entries}</dayList></trackDailyDistribution>').encode()


class CalendarXMLTests(TestCase):
    def test_namespaced_explicit_true_false(self):
        self.assertEqual(parse_month(month_xml({1: True, 2: False}), 2026, 10),
                         {1: True, 2: False})

    def test_invalid_xml_unexpected_error_and_unsupported_dates(self):
        samples = [
            b'<ResponseStatus><statusCode>1</statusCode></ResponseStatus>',
            b'<trackDailyDistribution><dayList/></trackDailyDistribution>',
            b'<trackDailyDistribution><day><dayOfMonth>35</dayOfMonth><record>true</record></day></trackDailyDistribution>',
            b'<trackDailyDistribution><day><dayOfMonth>1</dayOfMonth><record>maybe</record></day></trackDailyDistribution>',
            b'<trackDailyDistribution><day><dayOfMonth>1</dayOfMonth><record>true</record></day><day><dayOfMonth>1</dayOfMonth><record>false</record></day></trackDailyDistribution>',
            b'<trackDailyDistribution>',
        ]
        for content in samples:
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    parse_month(content, 2026, 10)

    @patch('app.retention.requests.Session')
    def test_query_uses_only_expected_digest_post(self, factory):
        session = factory.return_value.__enter__.return_value
        session.post.return_value.status_code = 200
        session.post.return_value.content = month_xml({4: True, 5: False})
        result = query_month(ip='192.0.2.10', port=80, username='x', password='y',
                             track_id=101, year=2026, month=10)
        self.assertEqual(result['status'], VERIFIED)
        self.assertEqual(result['days'][5], False)
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], 'http://192.0.2.10:80/ISAPI/ContentMgmt/record/tracks/101/dailyDistribution')
        self.assertIn(b'<year>2026</year>', kwargs['data'])
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'], (3, 8))
        self.assertFalse(session.trust_env)
        session.get.assert_not_called()

    @patch('app.retention.requests.Session')
    def test_unsupported_does_not_return_zero_days(self, factory):
        session = factory.return_value.__enter__.return_value
        session.post.return_value.status_code = 404
        self.assertEqual(query_month(ip='192.0.2.10', port=80, username='x', password='y',
                                      track_id=101, year=2026, month=10)['status'], UNSUPPORTED)

    @patch('app.retention.requests.Session')
    def test_malformed_response_inconclusive_not_absent(self, factory):
        session = factory.return_value.__enter__.return_value
        session.post.return_value.status_code = 200
        session.post.return_value.content = b'<ResponseStatus/>'
        result = query_month(ip='192.0.2.10', port=80, username='x', password='y',
                             track_id=101, year=2026, month=10)
        self.assertEqual(result['status'], INCONCLUSIVE)

    @patch('app.retention.requests.Session')
    def test_timeout_and_unauthorized_are_errors(self, factory):
        from requests.exceptions import Timeout
        session = factory.return_value.__enter__.return_value
        session.post.side_effect = Timeout()
        self.assertEqual(query_month(ip='192.0.2.10', port=80, username='x', password='y',
                                     track_id=101, year=2026, month=10)['status'], ERROR)
        session.post.side_effect = None
        session.post.return_value.status_code = 401
        self.assertEqual(query_month(ip='192.0.2.10', port=80, username='x', password='y',
                                     track_id=101, year=2026, month=10)['status'], ERROR)


class CalendarScanningTests(TestCase):
    def fake_query(self, **params):
        year, month = params['year'], params['month']
        import calendar
        return {'status': VERIFIED, 'days': {n: n % 2 == 0
                for n in range(1, calendar.monthrange(year, month)[1]+1)}}

    def test_cross_month_and_oldest_date_not_continuous_claim(self):
        visited = []
        def query(**params):
            visited.append((params['year'], params['month']))
            return self.fake_query(**params)
        result = scan_calendar(ip='192.0.2.10', port=80, username='x', password='y',
                               track_id=101, clock_snapshot=(NOW.astimezone(timezone.utc), TZ),
                               days=30, query=query)
        self.assertEqual(visited, [(2026, 9), (2026, 10)])
        self.assertEqual(result['status'], VERIFIED)
        self.assertEqual(len(result['calendar']), 30)
        self.assertEqual(result['days_examined'], 30)
        self.assertEqual(result['as_of_date'], '2026-10-28')
        self.assertEqual(result['calendar'][-1]['date'], '2026-10-27')
        self.assertNotIn('continuous', result['message'].lower())
        self.assertGreater(result['days_with_footage'], 0)
        self.assertEqual(result['oldest_verified_date'], '2026-09-28')

    def test_sparse_month_is_partial_not_zero(self):
        def query(**kwargs):
            return {'status': VERIFIED, 'days': {27: False}}
        result = scan_calendar(ip='192.0.2.10', port=80, username='x', password='y',
                               track_id=101, clock_snapshot=(NOW.astimezone(timezone.utc), TZ),
                               days=3, query=query)
        self.assertEqual(result['status'], PARTIAL)
        self.assertEqual(result['days_examined'], 1)
        self.assertEqual(result['days_with_footage'], 0)
        self.assertIsNone(result['oldest_verified_date'])
        self.assertIsNone(result['calendar'][0]['has_recording'])

    def test_explicit_no_recording_is_not_unknown(self):
        def query(**kwargs):
            return {'status': VERIFIED, 'days': {d: False for d in range(1, 32)}}
        result = scan_calendar(ip='192.0.2.10', port=80, username='x', password='y',
                               track_id=101, clock_snapshot=(NOW.astimezone(timezone.utc), TZ),
                               days=5, query=query)
        self.assertEqual(result['status'], VERIFIED)
        self.assertEqual(result['days_with_footage'], 0)
        self.assertIsNone(result['oldest_verified_age_days'])

    def test_error_on_one_month_prevents_partial_false_claim(self):
        def query(**kwargs):
            return {'status': VERIFIED, 'days': {1: True}} if kwargs['month'] == 9 else {'status': ERROR, 'message': 'timed out'}
        result = scan_calendar(ip='192.0.2.10', port=80, username='x', password='y',
                               track_id=101, clock_snapshot=(NOW.astimezone(timezone.utc), TZ),
                               days=30, query=query)
        self.assertEqual(result['status'], ERROR)
        self.assertIsNone(result['days_with_footage'])

    def test_date_limit_and_deadline_prevent_queries(self):
        from app.retention import _date_window
        with self.assertRaises(ValueError):
            _date_window(NOW, 91)
        query = MagicMock()
        result = scan_calendar(ip='192.0.2.10', port=80, username='x', password='y',
                               track_id=101, clock_snapshot=(NOW.astimezone(timezone.utc), TZ),
                               days=30, deadline=0, query=query)
        query.assert_not_called()
        self.assertEqual(result['status'], PENDING)

    def test_cache_and_stale_keep_prior_attempt(self):
        now = datetime.now(timezone.utc)
        old = {'track_id':101, 'status':VERIFIED, 'checked_at':now.isoformat(),
               'last_attempted_at':now.isoformat(), 'as_of_date':'2026-10-28',
               'window_days':30, 'days_with_footage':10, 'calendar':[],
               'today':{'date':'2026-10-28','has_recording':True}}
        self.assertTrue(cached_result(old, 101, now, 30, 168)['from_cache'])
        self.assertIsNone(cached_result(old, 201, now, 30, 168))
        self.assertIsNone(cached_result(old, 101, now+timedelta(days=10), 30, 168))
        # A calendar from an earlier day is never reused; one from today is.
        self.assertTrue(cached_result(old, 101, now, 30, 168, today=date(2026, 10, 28))['from_cache'])
        self.assertIsNone(cached_result(old, 101, now, 30, 168, today=date(2026, 10, 29)))
        before_today_field = {k: v for k, v in old.items() if k != 'today'}
        self.assertIsNone(cached_result(before_today_field, 101, now, 30, 168, today=date(2026, 10, 28)))
        unsupported = dict(old, status=UNSUPPORTED)
        self.assertTrue(cached_result(unsupported, 101, now, 30, 168, today=date(2026, 10, 29))['from_cache'])
        stale = deferred_result(old, 101, 'budget exhausted')
        self.assertEqual(stale['status'], STALE)
        self.assertEqual(stale['days_with_footage'], 10)
        self.assertIn('Earlier', stale['message'])


class RetentionIntegrationTests(TestCase):
    @patch('app.camera_recording.get_clock')
    @patch('app.camera_recording.get_tracks')
    def test_disabled_never_calls_new_calendar_endpoint(self, tracks, clock):
        tracks.return_value = {'status': 'VERIFIED', 'tracks': {101:'1', 201:'2'}}
        clock.return_value = NOW.astimezone(timezone.utc), TZ
        channels = [{'channel_id':'1','reported_status':'ONLINE'},
                    {'channel_id':'2','reported_status':'OFFLINE'}]
        def recent(**params):
            return {'status':'NO_MATCHES_IN_WINDOW','matches':[],'num_matches':0}
        calendar_query = MagicMock()
        summary = check_all_cameras(ip='192.0.2.10',port=80,username='x',password='y',
                                    channels=channels,enabled=True,search=recent,
                                    retention_scan=calendar_query)
        calendar_query.assert_not_called()
        self.assertEqual(summary['retention']['verified'], 0)
        self.assertEqual(channels[0]['retention_result']['status'], 'RETENTION_NOT_CONFIGURED')

    @patch('app.camera_recording.get_clock')
    @patch('app.camera_recording.get_tracks')
    def test_pilot_bounded_and_offline_included_and_cached(self, tracks, clock):
        tracks.return_value = {'status': 'VERIFIED', 'tracks': {101:'1', 201:'2', 301:'3'}}
        clock.return_value = NOW.astimezone(timezone.utc), TZ
        channels = [{'channel_id':str(i),'reported_status':('OFFLINE' if i==2 else 'ONLINE')}
                    for i in (1,2,3)]
        def recent(**params):
            return {'status':'NO_MATCHES_IN_WINDOW','matches':[],'num_matches':0}
        called=[]
        def retention_scan(**params):
            called.append(params['track_id'])
            return {'track_id':params['track_id'], 'status':VERIFIED,
                    'checked_at':datetime.now(timezone.utc).isoformat(),
                    'as_of_date':'2026-10-28', 'window_days':30,
                    'last_attempted_at':datetime.now(timezone.utc).isoformat(),
                    'calendar':[], 'days_with_footage':25,
                    'today':{'date':'2026-10-28','has_recording':True}}
        summary = check_all_cameras(ip='192.0.2.10',port=80,username='x',password='y',
                                    channels=channels,enabled=True,search=recent,
                                    retention_enabled=True,retention_max_cameras=2,
                                    retention_scan=retention_scan)
        self.assertEqual(called, [101,201])
        self.assertEqual(summary['retention']['verified'], 2)
        self.assertEqual(summary['retention']['pending'], 1)
        self.assertEqual(channels[1]['retention_result']['status'],VERIFIED)
        self.assertEqual(channels[2]['retention_result']['status'],PENDING)
        cached = {str(c['channel_id']):c['retention_result'] for c in channels}
        channels2 = [{'channel_id':str(i),'reported_status':'ONLINE'} for i in (1,2,3)]
        called.clear()
        check_all_cameras(ip='192.0.2.10',port=80,username='x',password='y',
                          channels=channels2,enabled=True,search=recent,
                          retention_enabled=True,retention_max_cameras=2,
                          previous_retention=cached, retention_scan=retention_scan)
        self.assertEqual(called, [301])
        # Next day: every calendar from yesterday is scanned again.
        clock.return_value = (NOW + timedelta(days=1)).astimezone(timezone.utc), TZ
        cached = {str(c['channel_id']): c['retention_result'] for c in channels2}
        called.clear()
        check_all_cameras(ip='192.0.2.10',port=80,username='x',password='y',
                          channels=[{'channel_id':str(i),'reported_status':'ONLINE'} for i in (1,2,3)],
                          enabled=True,search=recent, retention_enabled=True,retention_max_cameras=3,
                          previous_retention=cached, retention_scan=retention_scan)
        self.assertEqual(called, [101, 201, 301])

    def test_dashboard_sanitizes_dates_and_unknown_values(self):
        out = _public_retention({'status':VERIFIED, 'days_with_footage':3,
                                 'calendar':[{'date':'2026-10-27','has_recording':True},
                                             {'date':'not a date','has_recording':False},
                                             {'date':'2026-10-26','has_recording':'false'}],
                                 'password':'SECRET'})
        self.assertNotIn('password', out)
        self.assertEqual(out['days_with_footage'], 3)
        self.assertEqual(len(out['calendar']), 2)
        self.assertIsNone(out['calendar'][1]['has_recording'])


class TodaySquareTests(TestCase):
    """Today so far is shown separately; the 30 complete days are unchanged."""

    def scan(self, now, months):
        calls = []
        def query(**kw):
            calls.append((kw['year'], kw['month']))
            reply = months.get((kw['year'], kw['month']))
            return reply if reply is not None else {'status': ERROR, 'message': 'down'}
        result = scan_calendar(ip='192.0.2.10', port=80, username='x', password='y', track_id=101,
                               clock_snapshot=(now.astimezone(timezone.utc), TZ), days=30, query=query)
        return result, calls

    def test_today_comes_from_the_month_already_queried(self):
        november = {d: True for d in range(1, 31)}
        november[3] = False
        months = {(2026, 10): {'status': VERIFIED, 'days': {d: True for d in range(1, 32)}},
                  (2026, 11): {'status': VERIFIED, 'days': november}}
        result, calls = self.scan(datetime(2026, 11, 3, 8, 16, tzinfo=TZ), months)
        self.assertEqual(calls, [(2026, 10), (2026, 11)])
        self.assertEqual(result['today'], {'date': '2026-11-03', 'has_recording': False})
        self.assertEqual(result['calendar'][-1]['date'], '2026-11-02')
        self.assertEqual(len(result['calendar']), 30)
        self.assertEqual(result['days_with_footage'], 30)   # today never counts

    def test_first_of_month_asks_for_the_new_month_once(self):
        months = {(2026, 10): {'status': VERIFIED, 'days': {d: True for d in range(1, 32)}},
                  (2026, 11): {'status': VERIFIED, 'days': {1: True}}}
        result, calls = self.scan(datetime(2026, 11, 1, 6, 0, tzinfo=TZ), months)
        self.assertEqual(calls, [(2026, 10), (2026, 11)])
        self.assertEqual(result['today'], {'date': '2026-11-01', 'has_recording': True})

    def test_failed_today_query_keeps_the_calendar(self):
        months = {(2026, 10): {'status': VERIFIED, 'days': {d: True for d in range(1, 32)}}}
        result, _ = self.scan(datetime(2026, 11, 1, 6, 0, tzinfo=TZ), months)
        self.assertEqual(result['status'], VERIFIED)
        self.assertEqual(result['today'], {'date': '2026-11-01', 'has_recording': None})

    def test_dashboard_passes_only_a_valid_today(self):
        self.assertEqual(_public_retention({'status': VERIFIED, 'today': {'date': '2026-11-03', 'has_recording': True}})['today'],
                         {'date': '2026-11-03', 'has_recording': True})
        self.assertIsNone(_public_retention({'status': VERIFIED, 'today': {'date': 'x'}})['today'])
        self.assertIsNone(_public_retention({'status': VERIFIED})['today'])

