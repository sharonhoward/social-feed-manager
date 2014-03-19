import datetime
import gzip
import json
import re
import time

import requests
import tweepy
from tweepy.parsers import JSONParser
from tweepy.streaming import StreamListener

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models as m
from django.utils import timezone

from ui.utils import set_wait_time

RE_LINKS = re.compile(r'(https?://\S+)')
RE_MENTIONS = re.compile(u'(@[a-zA-z0-9_]+)')
RE_TWEET_ID = re.compile(r'.*statuses/([0-9]+)$')
RE_USER_NAME = re.compile(r'http://twitter.com/(.*)$')

DAY = datetime.timedelta(days=1)
dt = datetime.datetime(2012, 1, 1)
dt_end = datetime.datetime.today()
DATES = []
while dt < dt_end:
    DATES.append(dt)
    dt += DAY


def authenticated_api(username, api_root=None, parser=None):
    """Return an oauthenticated tweety API object."""
    auth = tweepy.OAuthHandler(settings.TWITTER_CONSUMER_KEY,
                               settings.TWITTER_CONSUMER_SECRET)
    try:
        user = User.objects.get(username=username)
        sa = user.social_auth.all()[0]
        auth.set_access_token(sa.tokens['oauth_token'],
                              sa.tokens['oauth_token_secret'])
        return tweepy.API(auth,
                          api_root=api_root or settings.TWITTER_API_ROOT,
                          parser=parser or JSONParser(),
                          secure=settings.TWITTER_USE_SECURE)
    except:
        return None


def dt_aware_from_created_at(created_at):
    ts = time.mktime(time.strptime(created_at, '%a %b %d %H:%M:%S +0000 %Y'))
    dt = datetime.datetime.fromtimestamp(ts)
    return timezone.make_aware(dt, timezone.utc)


class RotatingFile(StreamListener):

    def __init__(self, filename_prefix='data', save_interval_seconds=0,
                 data_dir='', compress=True):
        self.filename_prefix = filename_prefix
        self.save_interval_seconds = save_interval_seconds \
            or settings.SAVE_INTERVAL_SECONDS
        self.data_dir = data_dir or settings.DATA_DIR
        self.compress = compress
        self.start_time = time.time()
        self.fp = self._get_file()

    def on_data(self, data):
        self.fp.write('%s\n' % data)
        time_now = time.time()
        if time_now - self.start_time > self.save_interval_seconds:
            self.fp.close()
            self.fp = self._get_file()
            self.start_time = time_now

    def _get_file(self):
        if self.compress:
            return gzip.open(self._get_filename(), 'wb')
        else:
            return open(self._get_filename(), 'wb')

    def _get_filename(self):
        return '%s/%s-%s%s' % (self.data_dir, self.filename_prefix,
                               time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                             time.gmtime()),
                               '.gz' if self.compress else '')


class TwitterUserSet(m.Model):
    user = m.ForeignKey(User, related_name='sets')
    name = m.CharField(max_length=255)
    notes = m.TextField(blank=True, default='')

    def __unicode__(self):
        return '<Set: %s for user %s>' % (self.name, self.user.username)

    class Meta:
        ordering = ['user', 'name']
        unique_together = ['user', 'name']


class TwitterUser(m.Model):
    name = m.TextField(db_index=True)
    date_last_checked = m.DateTimeField(db_index=True, auto_now=True,
                                        help_text='Date twitter uid was \
                                                   last checked for \
                                                   username changes')
    uid = m.BigIntegerField(unique=True, blank=True, null=True)
    former_names = m.TextField(default='{}', blank=True)
    is_active = m.BooleanField(default=True)
    sets = m.ManyToManyField(TwitterUserSet, blank=True)

    def __unicode__(self):
        return 'user %s (sfm id %s)' % (self.name, self.id)

    @property
    def counts(self):
        return ','.join([str(dc.num_tweets) for dc in self.daily_counts.all()])

    def clean(self):
        # remove left whitespace, leading '@', and right whitespace
        self.name = self.name.lstrip().lstrip("@").rstrip()

        # check to prevent duplicates
        dups = TwitterUser.objects.filter(name=self.name)
        if dups:
            raise ValidationError('TwitterUser names must be unique. %s \
                                   is already present.' % self.name)

        # if is_active is (or is being changed to) False,
        # then skip the validation of the Twitter username,
        # We want to allow un-chcking is_active in cases where, e.g.
        # the Twitter account (and id) may have been deleted
        if self.is_active is False:
            return

        # look up user
        try:
            api = authenticated_api(username=settings.TWITTER_DEFAULT_USERNAME)
        # TODO: Verify whether this try/except is even necessary, given that
        # most of authenticated_api swallows its own exceptions (but will
        # return None, which is the basis for the "if api is None" clause
        # that follows.
        except tweepy.error.TweepError as e:
            raise ValidationError('Could not connect to Twitter \
                                   API using configured credentials. \
                                   Error: %s' % e)
        if api is None:
            raise ValidationError('Could not connect to Twitter \
                                   API using configured credentials.')
        try:
            user_status = api.get_user(screen_name=self.name)
        except tweepy.error.TweepError as e:
            if "'code': 34" in e.reason:
                raise ValidationError('Twitter screen name \'%s\' was not \
                                      found.' % self.name)
            if "'code': 32" in e.reason:
                raise ValidationError('Could not connect to Twitter \
                                       API using configured credentials.')

        self.uid = user_status['id']
        # use the screen name from twitter (may be capitalized differently)
        self.name = user_status['screen_name']


def populate_uid(name, force=False, api=None):
    """
    For a TwitterUser, populate its uid based on its stored screen name,
    if uid==0 (default value, indicating it hasn't been set yet).
    if force==True, do it even if uid isn't 0
    Only do this for active users.

    see https://dev.twitter.com/docs/api/1.1/get/users/lookup
       for explanation of get_user call
    see https://dev.twitter.com/docs/working-with-timelines
       for explanation of max_id, since_id usage
    see also:
       https://dev.twitter.com/docs/error-codes-responses
       https://dev.twitter.com/docs/rate-limiting
    """

    if api is None:
        api = authenticated_api(username=settings.TWITTER_DEFAULT_USERNAME)
    qs_tweeps = TwitterUser.objects.filter(is_active=True, name=name)
    for tweep in qs_tweeps:
        if tweep.uid == 0 or force is True:
            try:
                user_status = api.get_user(screen_name=name)
                tweep.uid = user_status['id']
                tweep.save()
                print 'updated user \'%s\' uid to %d' % (name, tweep.uid)
            except tweepy.error.TweepError as e:
                print 'Failed to find user \'%s\'. Error: %s' % (name, e)
            finally:
                time.sleep(set_wait_time(api.last_response))


class TwitterUserItem(m.Model):
    twitter_user = m.ForeignKey(TwitterUser, related_name='items')
    twitter_id = m.BigIntegerField(unique=True, default=0)
    date_published = m.DateTimeField(db_index=True)
    item_text = m.TextField(default='', blank=True)
    item_json = m.TextField(default='', blank=True)
    place = m.TextField(default='', blank=True)
    source = m.TextField(default='', blank=True)

    def __unicode__(self):
        return 'useritem (%s) %s' % (self.id, self.twitter_id)

    @property
    def twitter_url(self):
        return 'http://twitter.com/%s/status/%s' % (self.twitter_user.name,
                                                    self.twitter_id)

    @property
    def tweet(self):
        """Cache/return a parsed version of the json if available."""
        try:
            return self._parsed_tweet
        except:
            if self.item_json:
                self._parsed_tweet = json.loads(self.item_json)
            else:
                self._parsed_tweet = {}
            return self._parsed_tweet

    @property
    def text(self):
        try:
            return self.tweet['text']
        except:
            return self.item_text

    @property
    def mentions(self):
        return RE_MENTIONS.findall(self.text)

    @property
    def links(self):
        return RE_LINKS.findall(self.text)

    def is_retweet(self, strict=True):
        """A simple-minded attempt to catch RTs that aren't flagged
        by twitter proper with a retweeted_status.  This will catch
        some cases, others will slip through, e.g. quoted RTs in
        responses, or "RT this please".  Can't get them all. Likely
        heavily biased toward english."""
        if self.tweet.get('retweeted_status', False):
            return True
        if not strict:
            text_lower = self.tweet['text'].lower()
            if text_lower.startswith('rt '):
                return True
            if ' rt ' in text_lower:
                if not 'please rt' in text_lower \
                    and not 'pls rt' in text_lower \
                        and not 'plz rt' in text_lower:
                    return True
        return False

    def unshorten(self, url):
        """Don't try to guess; just resolve it, and follow 301s"""
        h = requests.get(url)
        stack = [i.url for i in h.history]
        stack.append(h.url)
        return stack

    @property
    def csv(self):
        """A list of values suitable for csv-ification"""
        r = [str(self.id),
             datetime.datetime.strftime(self.date_published,
                                        '%Y-%m-%dT%H:%M:%SZ'),
             datetime.datetime.strftime(self.date_published,
                                        '%m/%d/%Y'),
             self.tweet['id_str'],
             self.tweet['user']['screen_name'],
             str(self.tweet['user']['followers_count']),
             str(self.tweet['user']['friends_count']),
             str(self.tweet['retweet_count']),
             ', '.join([ht['text']
                        for ht in self.tweet['entities']['hashtags']]),
             self.tweet['in_reply_to_screen_name'] or '',
             ', '.join([m for m in self.mentions]),
             self.twitter_url,
             str(self.is_retweet()),
             str(self.is_retweet(strict=False)),
             self.tweet['text'].replace('\n', ' '),
             ]
        # only show up to two urls w/expansions
        for url in self.tweet['entities']['urls'][:2]:
            r.extend([url['url'], url['expanded_url']])
        return r


class TwitterUserTimelineJob(m.Model):
    date_started = m.DateTimeField(db_index=True, auto_now_add=True)
    date_finished = m.DateTimeField(db_index=True, auto_now=True)
    num_added = m.IntegerField(default=0)

    def __unicode__(self):
        return '<TwitterUserTimelineJob %s>' % self.id


class TwitterUserTimelineError(m.Model):
    job = m.ForeignKey(TwitterUserTimelineJob, related_name="errors")
    user = m.ForeignKey(TwitterUser)
    error = m.TextField(blank=True)


class TwitterFilter(m.Model):
    name = m.CharField(max_length=255, unique=True,
                       help_text="Name of this TwitterFilter")
    user = m.ForeignKey(User,
                        help_text="Account to use for authentication")
    is_active = m.BooleanField(default=False)
    words = m.TextField(blank=True,
                        help_text="""Keywords to track. Phrases of keywords \
are specified by a comma-separated list. See \
<a href="https://dev.twitter.com/docs/streaming-apis/parameters#track" \
onclick="window.open(this.href); return false;">the track parameter \
documentation</a> for more information.""")
    people = m.TextField(blank=True,
                         help_text="""A comma-separated list of user IDs, \
indicating the users to return statuses for in the stream. See the \
<a href="https://dev.twitter.com/docs/streaming-apis/parameters#follow" \
onclick="window.open(this.href); return false;">follow parameter \
documentation</a> for more information.""")
    locations = m.TextField(blank=True,
                            help_text="""
Specifies a set of bounding boxes to track. See the \
<a href="https://dev.twitter.com/docs/streaming-apis/parameters#locations" \
onclick="window.open(this.href); return false;">locations parameter \
documentation</a> for more information.""")

    def __unicode__(self):
        return '%s' % self.id
