#     Copyright 2014 Netflix, Inc.
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
"""
.. module: security_monkey.auditor
    :platform: Unix
    :synopsis: This class is subclassed to add audit rules.

.. version:: $$VERSION$$
.. moduleauthor:: Patrick Kelley <pkelley@netflix.com>

"""

import datastore

from security_monkey import app, db
from security_monkey.watcher import ChangeItem
from security_monkey.common.jinja import get_jinja_env
from security_monkey.datastore import User

import boto


class Auditor(object):
    """
    This class (and subclasses really) run a number of rules against the configurations
    and look for any violations.  These violations are saved with the object and a report
    is made available via the Web UI and through email.
    """
    index = None          # Should be overridden
    i_am_singular = None  # Should be overridden
    i_am_plural = None    # Should be overridden

    def __init__(self, accounts=None, debug=False):
        self.datastore = datastore.Datastore()
        self.accounts = accounts
        self.debug = debug
        self.items = []
        self.from_address = app.config.get('DEFAULT_MAIL_SENDER')
        self.team_emails = app.config.get('SECURITY_TEAM_EMAIL')
        self.emails = []
        self.emails.extend(self.team_emails)
        for account in self.accounts:
            users = User.query.filter(User.daily_audit_email==True).filter(User.accounts.any(name=accounts[0])).all()
            new_emails = [user.email for user in users]
            self.emails.extend(new_emails)

    def add_issue(self, score, issue, item, notes=None):
        """
        Adds a new issue to an item, if not already reported.
        :return: The new issue
        """
        for existing_issue in item.audit_issues:
            if existing_issue.issue == issue:
                if existing_issue.notes == notes:
                    if existing_issue.score == score:
                        app.logger.debug(
                            "Not adding issue because it was already found:{0}/{1}/{2}/{3}\n\t{4} -- {5}"
                            .format(item.index, item.region, item.account, item.name, issue, notes))
                        return existing_issue

        app.logger.debug("Adding issue: {0}/{1}/{2}/{3}\n\t{4} -- {5}"
                         .format(item.index, item.region, item.account, item.name, issue, notes))
        new_issue = datastore.ItemAudit(score=score,
                                        issue=issue,
                                        notes=notes,
                                        justified=False,
                                        justified_user_id=None,
                                        justified_date=None,
                                        justification=None)
        item.audit_issues.append(new_issue)
        return new_issue

    def audit_these_objects(self, items):
        """
        Only inspect the given items.
        """
        app.logger.debug("Asked to audit {0} Objects".format(len(items)))
        methods = [getattr(self, method_name) for method_name in dir(self) if method_name.find("check_") == 0]
        app.logger.debug("methods: {0}".format(methods))
        for item in items:
            for method in methods:
                method(item)
        self.items = items

    def audit_all_objects(self):
        """
        Read all items from the database and inspect them all.
        """
        self.items = self.read_previous_items()
        self.audit_these_objects(self.items)

    def read_previous_items(self):
        """
        Pulls the last-recorded configuration from the database.
        :return: List of all items for the given technology and the given account.
        """
        prev_list = []
        for account in self.accounts:
            prev = self.datastore.get_all_ctype_filtered(tech=self.index, account=account, include_inactive=False)
            # Returns a map of {Item: ItemRevision}
            for item in prev:
                item_revision = prev[item]
                new_item = ChangeItem(index=self.index,
                                      region=item.region,
                                      account=item.account.name,
                                      name=item.name,
                                      new_config=item_revision.config)
                new_item.audit_issues.extend(item.issues)
                prev_list.append(new_item)
        return prev_list

    def save_issues(self):
        """
        Save all new issues.  Delete all fixed issues.
        """
        app.logger.debug("\n\nSaving Issues.")
        for item in self.items:
            db_item = self.datastore._get_item(item.index, item.region, item.account, item.name)
            new_issues = item.audit_issues

            # Add new issues
            for new_issue in new_issues:
                nk = "{0} -- {1}".format(new_issue.issue, new_issue.notes)
                if nk not in ["{0} -- {1}".format(old_issue.issue, old_issue.notes) for old_issue in db_item.issues]:
                    app.logger.debug("Saving NEW issue {0}".format(nk))
                    db_item.issues.append(new_issue)
                    db.session.add(db_item)
                    db.session.add(new_issue)
                else:
                    key = "{0}/{1}/{2}/{3}".format(item.index, item.region, item.account, item.name)
                    app.logger.debug("Issue was previously found. Not overwriting.\n\t{0}\n\t{1}".format(key, nk))

            # Delete old issues
            for old_issue in db_item.issues:
                ok = "{0} -- {1}".format(old_issue.issue, old_issue.notes)
                if ok not in ["{0} -- {1}".format(new_issue.issue, new_issue.notes) for new_issue in new_issues]:
                    app.logger.debug("Deleting FIXED issue {0}".format(ok))
                    db.session.delete(old_issue)

        db.session.commit()
        #db.session.close()

    def email_report(self, report):
        """
        Given a report, send an email using SES.
        """
        if not report:
            app.logger.info("No Audit issues.  Not sending audit email.")
            return

        errors = []
        ses = boto.connect_ses()
        for email in self.emails:
            try:
                subject = "Security Monkey {0} Auditor Report".format(self.i_am_singular)
                ses.send_email(self.from_address, subject, report, email, format="html")
                app.logger.info("{0} Auditor Email sent to {1}".format(self.i_am_singular, email))
            except Exception, e:
                m = "Failed to email {0}: {1} / {2}".format(email, Exception, e)
                app.logger.critical(m)
                errors.append(m)
        if errors:
            message = "\n".join(errors)
            for email in self.team_emails:
                try:
                    subject = "Security Monkey: Issues Emailing {0} Auditor Report".format(self.i_am_singular)
                    ses.send_email(self.from_address, subject, message, email, format="html")
                except:
                    m = "Failed to email {0}: {1} / {2}".format(email, Exception, e)
                    app.logger.critical(m)

    def create_report(self):
        """
        Using a Jinja template (jinja_audit_email.html), create a report that can be emailed.
        :return: HTML - The output of the rendered template.
        """
        jenv = get_jinja_env()
        template = jenv.get_template('jinja_audit_email.html')
        # This template expects a list of items that have been sorted by total score in
        # decending order.
        for item in self.items:
            item.totalscore = 0
            for issue in item.audit_issues:
                item.totalscore = item.totalscore + issue.score
        sorted_list = sorted(self.items, key=lambda item: item.totalscore)
        sorted_list.reverse()
        report_list = []
        for item in sorted_list:
            if item.totalscore > 0:
                report_list.append(item)
            else:
                break
        if len(report_list) > 0:
            return template.render({'items': report_list})
        else:
            return False
