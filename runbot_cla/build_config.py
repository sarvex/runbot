# -*- encoding: utf-8 -*-

import glob
import io
import logging
import re

from odoo import models, fields

_logger = logging.getLogger(__name__)


class Step(models.Model):
    _inherit = "runbot.build.config.step"

    job_type = fields.Selection(selection_add=[('cla_check', 'Check cla')])

    def _run_cla_check(self, build, log_path):
        build._checkout()
        cla_glob = glob.glob(build._get_server_commit()._source_path("doc/cla/*/*.md"))
        error = False
        if cla_glob:
            checked = set()
            for commit in build.params_id.commit_ids:
                email = commit.author_email
                if email in checked:
                    continue
                checked.add(email)
                build._log(
                    'check_cla',
                    f"[Odoo CLA signature](https://www.odoo.com/sign-cla) check for {commit.author} ({email}) ",
                    log_type='markdown',
                )
                if mo := re.search('[^ <@]+@[^ @>]+', email or ''):
                    email = mo[0].lower()
                    if not re.match('.*@(odoo|openerp|tinyerp)\.com$', email):
                        try:
                            cla = ''.join(io.open(f, encoding='utf-8').read() for f in cla_glob)
                            if email not in cla.lower():
                                error = True
                                build._log('check_cla', f'Email not found in cla file {email}', level="ERROR")
                        except UnicodeDecodeError:
                            error = True
                            build._log('check_cla', 'Invalid CLA encoding (must be utf-8)', level="ERROR")
                else:
                    error = True
                    build._log('check_cla', f'Invalid email format {email}', level="ERROR")
        else:
            error = True
            build._log('check_cla', "Missing cla file", level="ERROR")

        if error:
            build.local_result = 'ko'
        elif not build.local_result:
            build.local_result = 'ok'
