import os
import logging

from google.appengine.ext import db
from google.appengine.api import users
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp.util import login_required

#from jinja2 import Environment, FileSystemLoader, TemplateNotFound
from google.appengine.ext.webapp import template

import markdown

from time import sleep
from tools import slugify, decode
from models import *

# Setup jinja templating
tdir = os.path.join(os.path.dirname(__file__), '../templates/')
#env = Environment(loader=FileSystemLoader(template_dirs))


# OpenID Login
class LogIn(webapp.RequestHandler):
    def get(self):
        #user = users.get_current_user()
        action = self.request.get('action')
        target_url = self.request.get('continue')
        if action and action == "verify":
            f = self.request.get('openid_identifier')
            url = users.create_login_url(target_url, federated_identity=f)
            self.redirect(url)
        else:
            self.response.out.write(template.render(tdir + "login.html", \
                    {"continue_to": target_url}))


class LogOut(webapp.RequestHandler):
    def get(self):
        url = users.create_logout_url("/")
        self.redirect(url)


# Custom sites
class Main(webapp.RequestHandler):
    def get(self):
        user = users.get_current_user()
        prefs = UserPrefs.from_user(user)

        q = Snippet.all()
        q.order("-date_submitted")
        snippets = q.fetch(20)

        values = {'prefs': prefs, 'snippets': snippets}
        self.response.out.write(template.render(tdir + "index.html", values))


class Account(webapp.RequestHandler):
    def get(self):
        user = users.get_current_user()
        prefs = UserPrefs.from_user(user)


class LegacySnippetView(webapp.RequestHandler):
    """Redirects for snippets of legacy androidsnippets.org, which have
    links such as http://androidsnippets.org/snippets/198"""
    def get(self, legacy_slug):
        q = Snippet.all()
        q.filter("slug2 =", legacy_slug)
        snippet = q.get()

        if not snippet:
            # Show snippet-not-found.html
            user = users.get_current_user()
            prefs = UserPrefs.from_user(user)

            values = {'prefs': prefs}
            self.response.out.write(template.render(tdir + \
                "snippets_notfound.html", values))
            return

        self.redirect("/%s" % snippet.slug1, permanent=True)


class SnippetView(webapp.RequestHandler):
    def get(self, snippet_slug):
        user = users.get_current_user()
        prefs = UserPrefs.from_user(user)

        q = Snippet.all()
        q.filter("slug1 =", snippet_slug)
        snippet = q.get()

        if not snippet:
            # Show snippet-not-found.html
            values = {'prefs': prefs, "q": snippet_slug}
            self.response.out.write(template.render(tdir + \
                "snippets_notfound.html", values))
            return

        # Todo: temporarily store in memcache, update with cron
        snippet.views += 1
        snippet.save()

        # get all open revisions
        revisions = SnippetRevision.all()
        revisions.filter("snippet =", snippet)
        revisions.filter("merged =", False)
        revisions.filter("rejected =", False)
        revisions.order("-date_submitted")

        # but only include merged edits
        accepted_revisions = SnippetRevision.all()
        accepted_revisions.filter("snippet =", snippet)
        accepted_revisions.filter("merged =", True)
        accepted_revisions.order("date_submitted")

        has_voted = False
        # see if user has voted
        if user:
            q1 = db.GqlQuery("SELECT * FROM SnippetUpvote WHERE \
                    userprefs = :1 and snippet = :2", prefs, snippet)
            q2 = db.GqlQuery("SELECT * FROM SnippetDownvote WHERE \
                    userprefs = :1 and snippet = :2", prefs, snippet)
            has_voted = q1.count() or -q2.count()  # 0 if not, 1, -1

        desc_md = markdown.markdown(snippet.description)
        values = {"prefs": prefs, "snippet": snippet, "revisions": revisions, \
                'voted': has_voted, 'accepted_revisions': accepted_revisions,
                "openedit": self.request.get('edit'), 'desc_md': desc_md}
        self.response.out.write(template.render(tdir + \
            "snippets_view.html", values))


class SnippetVote(webapp.RequestHandler):
    def get(self, snippet_slug):
        user = users.get_current_user()
        prefs = UserPrefs.from_user(user)

        if not user:
            self.response.out.write("-1")
            return

        q = Snippet.all()
        q.filter("slug1 =", snippet_slug)
        snippet = q.get()

        if not snippet:
            self.error(404)
            return

        # Check if user has already voted
        q = SnippetUpvote.all()
        q.filter("userprefs = ", prefs)
        q.filter("snippet =", snippet)
        vote = q.get()

        if vote:
            # Has already voted
            # self.response.out.write("0")
            pass
        else:
            # Create the upvote
            u = SnippetUpvote(userprefs=prefs, snippet=snippet)
            u.save()
            snippet.upvote()
            snippet.save()
            # self.response.out.write("1")

        self.redirect("/%s" % snippet_slug)


class SnippetEdit(webapp.RequestHandler):
    """TODO: Check if all form input is here, if user is allowed, etc"""
    def post(self, snippet_slug):
        user = users.get_current_user()
        prefs = UserPrefs.from_user(user)

        title = decode(self.request.get('title'))
        code = decode(self.request.get('code'))
        description = decode(self.request.get('description'))
        tags = decode(self.request.get('tags'))

        if not title or not code or not description:
            self.response.out.write("-1")
            return

        q = Snippet.all()
        q.filter("slug1 =", snippet_slug)
        snippet = q.get()

        if not snippet:
            self.error(404)
            return

        # Create a new revision
        r = SnippetRevision(userprefs=prefs, snippet=snippet)
        r.title = title
        r.description = description
        r.code = code
        r.put()

        if user == snippet.userprefs.user:
            # Auto-merge new revision if edit by author
            r.merge(merged_by=prefs)
            # self.response.out.write("0")
        else:
            # Add proposal info if from another editor
            snippet.proposal_count += 1
            snippet.date_lastproposal = datetime.datetime.now()
            snippet.date_lastactivity = datetime.datetime.now()
            snippet.save()
            # self.response.out.write("1")

        self.redirect("/%s" % snippet_slug)


class SnippetEditView(webapp.RequestHandler):
    """Popup that shows an edit from another user"""
    def get(self, snippet_slug, rev_key):
        user = users.get_current_user()
        prefs = UserPrefs.from_user(user)

        rev = SnippetRevision.get(rev_key)
        #q = db.GqlQuery("SELECT * FROM SnippetRevision WHERE __key__ = :1", \
        #        db.Key(rev_key))
        #rev = q.get()  # fetch(1)[0]

        if not rev:
            self.error(404)
            return

        # TODO: memcache?
        rev.views += 1
        rev.put()

        has_voted = False
        if user:
            # see if user has already voted
            q1 = db.GqlQuery("SELECT * FROM SnippetRevisionUpvote WHERE \
                    userprefs = :1 and snippetrevision = :2", prefs, rev)
            q2 = db.GqlQuery("SELECT * FROM SnippetRevisionDownvote WHERE \
                    userprefs = :1 and snippetrevision = :2", prefs, rev)
            has_voted = q1.count() or -q2.count()  # 0 if not, 1, -1

            if not has_voted:
                # Check if user wantsto vote
                has_voted = decode(self.request.get('v'))
                if has_voted == "1":
                    # user wants to upvote
                    v = SnippetRevisionUpvote(userprefs=prefs, \
                            snippetrevision=rev)
                    v.save()
                elif has_voted == "-1":
                    # user downvotes
                    v = SnippetRevisionDownvote(userprefs=prefs, \
                            snippetrevision=rev)
                    v.save()

        # TODO: memcache
        desc_md = markdown.markdown(rev.description)
        values = {"prefs": prefs, "rev": rev, 'voted': str(has_voted), \
                'desc_md': desc_md}
        self.response.out.write(template.render(tdir + \
            "snippets_edit_view.html", values))