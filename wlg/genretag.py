#!/usr/bin/env python
'''whatlastgenre genretag'''

from __future__ import division, print_function

import ConfigParser
import StringIO
from _collections import defaultdict
import difflib
import itertools
import logging
import pkgutil
import re


LOG = logging.getLogger('whatlastgenre')


class GenreTags(object):
    '''Class for managing the genre tags.'''

    def __init__(self, conf):
        from wlg.whatlastgenre import get_conf_list
        self.conf = conf
        self.tags = None
        filters = ['badtags', 'generic']
        filters += get_conf_list(conf, 'genres', 'filters')
        # tags file parsing
        self.parser = ConfigParser.SafeConfigParser(allow_no_value=True)
        tagfp = StringIO.StringIO(pkgutil.get_data('wlg', 'tags.txt'))
        self.parser.readfp(tagfp)
        # tags file validation
        for sec in ['basictags', 'uppercase', 'splitpart', 'dontsplit',
                    'replaceme']:
            if not self.parser.has_section(sec):
                print("Got no [%s] from tag.txt file." % sec)
                exit()
        for sec in filters:
            if not (self.parser.has_section('filter_%s' % sec) or
                    self.parser.has_section('filter_%s_fuzzy' % sec)):
                print("The configured filter '%s' doesn't have a "
                      "[filter_%s[_fuzzy]] section in the tags.txt file."
                      % (sec, sec))
                exit()
        # set up matchlist
        self.matchlist = self.parser.options('basictags')
        self.matchlist += get_conf_list(conf, 'genres', 'love')
        self.matchlist += get_conf_list(conf, 'genres', 'hate')
        self.matchlist += get_conf_list(conf, 'genres', 'blacklist')
        # set up replaces
        self.replaces = {}
        for pattern, repl in self.parser.items("replaceme", True):
            self.replaces.update({pattern: repl})
        # set up regex
        self.regex = {}
        # compile config options
        for sec in ['love', 'hate']:
            pat = '(%s)$' % '|'.join(get_conf_list(conf, 'genres', sec))
            self.regex[sec] = re.compile(pat, re.I)
        # compile tagsfile sections
        for sec in ['splitpart', 'dontsplit', 'replaceme']:
            pat = '(%s)$' % '|'.join(self.parser.options(sec))
            self.regex[sec] = re.compile(pat, re.I)
        # build filter
        filter_ = get_conf_list(conf, 'genres', 'blacklist')
        for sec in [s for s in self.parser.sections()
                    if s.startswith('filter_')]:
            if sec[7:] in filters:
                filter_ += self.parser.options(sec)
            elif sec.endswith('_fuzzy') and sec[7:-6] in filters:
                for tag in self.parser.options(sec):
                    filter_.append('.*%s.*' % tag)
        # compile filter in chunks
        self.regex['filter'] = []
        for i in range(0, len(filter_), 256):
            pat = '(%s)$' % '|'.join(filter_[i:i + 256])
            self.regex['filter'].append(re.compile(pat, re.I))

    def _add(self, group, name, score):
        '''Adds a genre tag after some filter, replace, match, split.'''
        name = name.encode('ascii', 'ignore').lower()
        if self._filter(name) or not score:
            return
        name = self._replace(name)
        if self._filter(name):
            return
        name = self._match(name)
        score = self._split(group, name, score)
        if not score:
            return
        self.tags[group][name] += score

    def _filter(self, tagname):
        '''Filters a tag by name, returns True if tag got filtered.'''
        if len(tagname) < 3 or len(tagname) > 19:
            return True
        if self.regex['filter_album'].match(tagname):
            return True
        for filter_ in self.regex['filter']:
            if filter_.match(tagname):
                return True
        return False

    def _replace(self, tagname):
        '''Applies all the replaces to a tagname.'''
        tagname = re.sub(r'([_/\\,;\.\+\*]| and )', '&', tagname, 0, re.I)
        # tagname = re.sub(r'-', ' ', tagname)  # ?
        tagname = re.sub(r'[^a-z0-9&\- ]', '', tagname, 0, re.I)
        if self.regex['replaceme'].match(tagname):
            for pattern, repl in self.replaces.items():
                tagname = re.sub(pattern, repl, tagname, 0, re.I)
        tagname = re.sub('( +|_)', ' ', tagname).strip()
        return tagname

    def _match(self, tagname):
        '''Matches tagname with existing tags.'''
        mli = []
        for taglist in self.tags.values():
            mli += taglist.keys()
        mli += self.matchlist
        # don't change cutoff, _add replaces instead
        match = difflib.get_close_matches(tagname, mli, 1, .8572)
        if match:
            return match[0]
        return tagname

    def _split(self, group, name, score):
        '''Splits a tag and adds its parts with modified score,
        returns the remaining score for the base tag.'''
        if self.regex['dontsplit'].match(name):
            return score
        if '&' in name:
            for part in name.split('&'):
                if not self._filter(part):
                    self._add(group, part, score)
                return None
        if ' ' in name.strip():
            split = name.split(' ')
            parts = [p for p in split if not self._filter(p)]
            splitup = False
            for part in itertools.combinations(parts, max(1, len(parts) - 1)):
                splitup = True
                self._add(group, ' '.join(part), score)
            if len(split) > 2:
                return None
            if len(parts) != len(split):
                return None
            if splitup:
                return score * self.conf.getfloat('scores', 'splitup')
        return score

    def reset(self, bot):
        '''Resets the genre tags and album filter.'''
        self.tags = {'artist': defaultdict(float), 'album': defaultdict(float)}
        self.regex['filter_album'] = self.get_album_filter(bot)

    def add_tags(self, tags, source, part):
        '''Adds tags with or without counts to a given part, scores them
        while taking the source score multiplier into account.'''
        if not tags:
            return
        multi = self.conf.getfloat('scores', 'src_%s' % source)
        if isinstance(tags, dict):
            for name, score in tags.items():
                self._add(part, name, multi * score / max(tags.values()))
        elif isinstance(tags, list):
            for name in tags:
                self._add(part, name, .85 ** (len(tags) - 1) * multi)

    def get(self, various=False):
        '''Returns the sorted and formated genre tags after merging.'''
        for group, grptags in self.tags.items():
            if not grptags:
                continue
            # norm tag scores
            for tag, score in grptags.items():
                grptags[tag] = score / max(grptags.values())
            # verbose output
            toptags = ', '.join(["%s (%.2f)" % (self.format(k), v) for k, v in
                             sorted(grptags.items(), key=lambda (k, v):
                                    (v, k), reverse=1)][:10])
            LOG.info("Best %6s tags (%d): %s", group, len(grptags), toptags)
        # merge artist and album tags
        tags = defaultdict(float)
        for group, grptags in self.tags.items():
            mult = 1
            if group == 'artist':
                if various:
                    mult = self.conf.getfloat('scores', 'various')
                else:
                    mult = self.conf.getfloat('scores', 'artist')
            for tag, score in grptags.items():
                # score bonus
                if self.regex['love'].match(tag):
                    score *= 2
                elif self.regex['hate'].match(tag):
                    score *= 0.5
                tags[tag] += score * mult
        # format and sort
        tags = {self.format(k): v for k, v in tags.items()}
        return sorted(tags, key=tags.get, reverse=True)

    def format(self, name):
        '''Formats a tag to correct case.'''
        split = name.split(' ')
        for i in range(len(split)):
            if len(split[i]) < 3 and split[i] != 'nu' or \
                    split[i] in self.parser.options('uppercase'):
                split[i] = split[i].upper()
            elif re.match('[0-9]{4}s', name, re.I):
                split[i] = split[i].lower()
            else:
                split[i] = split[i].title()
        return ' '.join(split)

    @classmethod
    def get_album_filter(cls, bot):
        ''' Returns a genre tag filter based on
        the metadata of a given bunch of tracks.'''
        badtags = []
        for tag in ['albumartist', 'album']:
            val = bot.get_common_meta(tag)
            if not val:
                continue
            bts = [val]
            if tag == 'albumartist' and ' ' in bts[0]:
                bts += bts[0].split(' ')
            for badtag in bts:
                for pat in [r'\(.*\)', r'\[.*\]', '{.*}', '-.*-', "'.*'",
                            '".*"', r'vol(\.|ume)? ', ' and ', 'the ',
                            r'[\W\d]', r'(\.\*)+']:
                    badtag = re.sub(pat, '.*', badtag, 0, re.I).strip()
                badtag = re.sub(r'(^\.\*|\.\*$)', '', badtag, 0, re.I)
                if len(badtag) > 2:
                    badtags.append(badtag.strip().lower())
        return re.compile('.*(' + '|'.join(badtags) + ').*', re.I)

