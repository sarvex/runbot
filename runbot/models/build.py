# -*- coding: utf-8 -*-
import fnmatch
import logging
import pwd
import re
import shutil
import subprocess
import time
import datetime
import hashlib
from ..common import dt2time, fqdn, now, grep, local_pgadmin_cursor, s2human, dest_reg, os, list_local_dbs, pseudo_markdown, RunbotException
from ..container import docker_stop, docker_state, Command, docker_run
from ..fields import JsonDictField
from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError
from odoo.http import request
from odoo.tools import appdirs
from odoo.tools.safe_eval import safe_eval
from collections import defaultdict
from psycopg2 import sql
from subprocess import CalledProcessError

_logger = logging.getLogger(__name__)

result_order = ['ok', 'warn', 'ko', 'skipped', 'killed', 'manually_killed']
state_order = ['pending', 'testing', 'waiting', 'running', 'done']

COPY_WHITELIST = [
    "params_id",
    "description",
    "build_type",
    "parent_id",
    "orphan_result",
]


def make_selection(array):
    return [(elem, elem.replace('_', ' ').capitalize()) if isinstance(elem, str) else elem for elem in array]


class BuildParameters(models.Model):
    _name = 'runbot.build.params'
    _description = "All information used by a build to run, should be unique and set on create only"

    # on param or on build?
    # execution parametter
    commit_link_ids = fields.Many2many('runbot.commit.link', copy=True)
    commit_ids = fields.Many2many('runbot.commit', compute='_compute_commit_ids')
    version_id = fields.Many2one('runbot.version', required=True, index=True)
    project_id = fields.Many2one('runbot.project', required=True, index=True)  # for access rights
    trigger_id = fields.Many2one('runbot.trigger', index=True)  # for access rights
    create_batch_id = fields.Many2one('runbot.batch')
    category = fields.Char('Category', index=True)  # normal vs nightly vs weekly, ...
    dockerfile_id = fields.Many2one('runbot.dockerfile', index=True, default=lambda self: self.env.ref('runbot.docker_default', raise_if_not_found=False))
    skip_requirements = fields.Boolean('Skip requirements.txt auto install')
    # other informations
    extra_params = fields.Char('Extra cmd args')
    config_id = fields.Many2one('runbot.build.config', 'Run Config', required=True,
                                default=lambda self: self.env.ref('runbot.runbot_build_config_default', raise_if_not_found=False), index=True)
    config_data = JsonDictField('Config Data')

    build_ids = fields.One2many('runbot.build', 'params_id')
    builds_reference_ids = fields.Many2many('runbot.build', relation='runbot_build_params_references', copy=True)
    modules = fields.Char('Modules')

    upgrade_to_build_id = fields.Many2one('runbot.build', index=True)  # use to define sources to use with upgrade script
    upgrade_from_build_id = fields.Many2one('runbot.build', index=True)  # use to download db
    dump_db = fields.Many2one('runbot.database', index=True)  # use to define db to download

    fingerprint = fields.Char('Fingerprint', compute='_compute_fingerprint', store=True, index=True)

    _sql_constraints = [
        ('unique_fingerprint', 'unique (fingerprint)', 'avoid duplicate params'),
    ]

    # @api.depends('version_id', 'project_id', 'extra_params', 'config_id', 'config_data', 'modules', 'commit_link_ids', 'builds_reference_ids')
    def _compute_fingerprint(self):
        for param in self:
            if param.trigger_id.batch_dependent:
                cleaned_vals = {
                    'version_id': param.version_id.id,
                    'project_id': param.project_id.id,
                    'trigger_id': param.trigger_id.id,
                    'extra_params': param.extra_params or '',
                    'config_id': param.config_id.id,
                    'config_data': param.config_data.dict,
                    'modules': param.modules or '',
                    'commit_link_ids': sorted(param.commit_link_ids.commit_id.ids),
                    'builds_reference_ids': sorted(param.builds_reference_ids.ids),
                    'upgrade_from_build_id': param.upgrade_from_build_id.id,
                    'upgrade_to_build_id': param.upgrade_to_build_id.id,
                    'dump_db': param.dump_db.id,
                    'dockerfile_id': param.dockerfile_id.id,
                    'skip_requirements': param.skip_requirements,
                }
                    cleaned_vals['create_batch_id'] = param.create_batch_id.id,
                param.fingerprint = hashlib.sha256(str(cleaned_vals).encode('utf8')).hexdigest()

    @api.depends('commit_link_ids')
    def _compute_commit_ids(self):
        for params in self:
            params.commit_ids = params.commit_link_ids.commit_id

    def create(self, values):
        params = self.new(values)
        if match := self._find_existing(params.fingerprint):
            return match
        values = self._convert_to_write(params._cache)
        return super().create(values)

    def _find_existing(self, fingerprint):
        return self.env['runbot.build.params'].search([('fingerprint', '=', fingerprint)], limit=1)

    def write(self, vals):
        raise UserError('Params cannot be modified')


class BuildResult(models.Model):
    # remove duplicate management
    # instead, link between bundle_batch and build
    # kill -> only available from bundle.
    # kill -> actually detach the build from the bundle
    # rebuild: detach and create a new link (a little like exact rebuild),
    # if a build is detached from all bundle, kill it
    # nigktly?

    _name = 'runbot.build'
    _description = "Build"

    _parent_store = True
    _order = 'id desc'
    _rec_name = 'id'

    # all displayed info removed. How to replace that?
    # -> commit corresponding to repo of trigger_id5
    # -> display all?

    params_id = fields.Many2one('runbot.build.params', required=True, index=True, auto_join=True)
    no_auto_run = fields.Boolean('No run')
    # could be a default value, but possible to change it to allow duplicate accros branches

    description = fields.Char('Description', help='Informative description')
    md_description = fields.Char(compute='_compute_md_description', String='MD Parsed Description', help='Informative description markdown parsed')
    display_name = fields.Char(compute='_compute_display_name')

    # Related fields for convenience
    version_id = fields.Many2one('runbot.version', related='params_id.version_id', store=True, index=True)
    config_id = fields.Many2one('runbot.build.config', related='params_id.config_id', store=True, index=True)
    trigger_id = fields.Many2one('runbot.trigger', related='params_id.trigger_id', store=True, index=True)

    # state machine
    global_state = fields.Selection(make_selection(state_order), string='Status', compute='_compute_global_state', store=True)
    local_state = fields.Selection(make_selection(state_order), string='Build Status', default='pending', required=True, index=True)
    global_result = fields.Selection(make_selection(result_order), string='Result', compute='_compute_global_result', store=True)
    local_result = fields.Selection(make_selection(result_order), string='Build Result')
    triggered_result = fields.Selection(make_selection(result_order), string='Triggered Result')  # triggered by db only

    requested_action = fields.Selection([('wake_up', 'To wake up'), ('deathrow', 'To kill')], string='Action requested', index=True)
    # web infos
    host = fields.Char('Host')
    keep_host = fields.Boolean('Keep host on rebuild and for children')

    port = fields.Integer('Port')
    dest = fields.Char(compute='_compute_dest', type='char', string='Dest', readonly=1, store=True)
    domain = fields.Char(compute='_compute_domain', type='char', string='URL')
    # logs and stats
    log_ids = fields.One2many('ir.logging', 'build_id', string='Logs')
    error_log_ids = fields.One2many('ir.logging', 'build_id', domain=[('level', 'in', ['WARNING', 'ERROR', 'CRITICAL'])], string='Error Logs')
    stat_ids = fields.One2many('runbot.build.stat', 'build_id', strings='Statistics values')
    log_list = fields.Char('Comma separted list of step_ids names with logs', compute="_compute_log_list", store=True)

    active_step = fields.Many2one('runbot.build.config.step', 'Active step')
    job = fields.Char('Active step display name', compute='_compute_job')
    job_start = fields.Datetime('Job start')
    job_end = fields.Datetime('Job end')
    build_start = fields.Datetime('Build start')
    build_end = fields.Datetime('Build end')
    docker_start = fields.Datetime('Docker start')
    job_time = fields.Integer(compute='_compute_job_time', string='Job time')
    build_time = fields.Integer(compute='_compute_build_time', string='Build time')

    gc_date = fields.Datetime('Local cleanup date', compute='_compute_gc_date')
    gc_delay = fields.Integer('Cleanup Delay', help='Used to compute gc_date')

    build_age = fields.Integer(compute='_compute_build_age', string='Build age')

    coverage = fields.Boolean('Code coverage was computed for this build')
    coverage_result = fields.Float('Coverage result', digits=(5, 2))
    build_type = fields.Selection([('scheduled', 'This build was automatically scheduled'),
                                   ('rebuild', 'This build is a rebuild'),
                                   ('normal', 'normal build'),
                                   ('indirect', 'Automatic rebuild'), # TODO cleanup remove
                                   ],
                                  default='normal',
                                  string='Build type')

    # what about parent_id and duplmicates?
    # -> always create build, no duplicate? (make sence since duplicate should be the parent and params should be inherited)
    # -> build_link ?

    parent_id = fields.Many2one('runbot.build', 'Parent Build', index=True)
    parent_path = fields.Char('Parent path', index=True)
    top_parent =  fields.Many2one('runbot.build', compute='_compute_top_parent')
    ancestors =  fields.Many2many('runbot.build', compute='_compute_ancestors')
    # should we add a has children stored boolean?
    children_ids = fields.One2many('runbot.build', 'parent_id')

    # config of top_build is inherithed from params, but subbuild will have different configs

    orphan_result = fields.Boolean('No effect on the parent result', default=False)

    build_url = fields.Char('Build url', compute='_compute_build_url', store=False)
    build_error_ids = fields.Many2many('runbot.build.error', 'runbot_build_error_ids_runbot_build_rel', string='Errors')
    keep_running = fields.Boolean('Keep running', help='Keep running', index=True)
    log_counter = fields.Integer('Log Lines counter', default=100)

    slot_ids = fields.One2many('runbot.batch.slot', 'build_id')
    killable = fields.Boolean('Killable')

    database_ids = fields.One2many('runbot.database', 'build_id')

    static_run = fields.Char('Static run URL')

    @api.depends('description', 'params_id.config_id')
    def _compute_display_name(self):
        for build in self:
            build.display_name = build.description or build.config_id.name

    @api.depends('params_id.config_id')
    def _compute_log_list(self):  # storing this field because it will be access trhoug repo viewn and keep track of the list at create
        for build in self:
            build.log_list = ','.join({step.name for step in build.params_id.config_id.step_ids() if step._has_log()})
        # TODO replace logic, add log file to list when executed (avoid 404, link log on docker start, avoid fake is_docker_step)

    @api.depends('children_ids.global_state', 'local_state')
    def _compute_global_state(self):
        for record in self:
            waiting_score = record._get_state_score('waiting')
            children_ids = [child for child in record.children_ids if not child.orphan_result]
            if record._get_state_score(record.local_state) > waiting_score and children_ids:  # if finish, check children
                children_state = record._get_youngest_state([child.global_state for child in children_ids])
                if record._get_state_score(children_state) > waiting_score:
                    record.global_state = record.local_state
                else:
                    record.global_state = 'waiting'
            else:
                record.global_state = record.local_state

    @api.depends('gc_delay', 'job_end')
    def _compute_gc_date(self):
        icp = self.env['ir.config_parameter']
        max_days_main = int(icp.get_param('runbot.db_gc_days', default=30))
        max_days_child = int(icp.get_param('runbot.db_gc_days_child', default=15))
        for build in self:
            ref_date = fields.Datetime.from_string(build.job_end or build.create_date or fields.Datetime.now())
            max_days = (
                max_days_main if not build.parent_id else max_days_child
            ) + int(build.gc_delay if build.gc_delay else 0)
            build.gc_date = ref_date + datetime.timedelta(days=(max_days))

    @api.depends('description')
    def _compute_md_description(self):
        for build in self:
            build.md_description = pseudo_markdown(build.description)

    def _compute_top_parent(self):
        for build in self:
            build.top_parent = self.browse(int(build.parent_path.split('/')[0]))

    def _compute_ancestors(self):
        for build in self:
            build.ancestors = self.browse([int(b) for b in build.parent_path.split('/') if b])

    def _get_youngest_state(self, states):
        index = min(self._get_state_score(state) for state in states)
        return state_order[index]

    def _get_state_score(self, result):
        return state_order.index(result)

    @api.depends('children_ids.global_result', 'local_result', 'children_ids.orphan_result')
    def _compute_global_result(self):
        for record in self:
            if record.local_result and record._get_result_score(record.local_result) >= record._get_result_score('ko'):
                record.global_result = record.local_result
            elif children_ids := [
                child for child in record.children_ids if not child.orphan_result
            ]:
                children_result = record._get_worst_result([child.global_result for child in children_ids], max_res='ko')
                record.global_result = (
                    record._get_worst_result(
                        [record.local_result, children_result]
                    )
                    if record.local_result
                    else children_result
                )
            else:
                record.global_result = record.local_result

    def _get_worst_result(self, results, max_res=False):
        results = [result for result in results if result]  # filter Falsy values
        index = max((self._get_result_score(result) for result in results), default=0)
        if max_res:
            return result_order[min([index, self._get_result_score(max_res)])]
        return result_order[index]

    def _get_result_score(self, result):
        return result_order.index(result)

    @api.depends('active_step')
    def _compute_job(self):
        for build in self:
            build.job = build.active_step.name

    def copy_data(self, default=None):
        values = super().copy_data(default)[0] or {}
        default = dict(default or [])
        values = {
            key: value
            for key, value in values.items()
            if (key in COPY_WHITELIST or key in default)
        } | {
            'host': 'PAUSED',  # hack to keep the build in pending waiting for a manual update. Todo: add a paused flag instead
            'local_state': 'pending',
        }
        return [values]

    def write(self, values):
        # some validation to ensure db consistency
        if 'local_state' in values:
            build_by_old_values = defaultdict(lambda: self.env['runbot.build'])
            for record in self:
                build_by_old_values[record.local_state] += record
        local_result = values.get('local_result')
        for build in self:
            if local_result and local_result != self._get_worst_result([build.local_result, local_result]):  # dont write ok on a warn/error build
                if len(self) == 1:
                    values.pop('local_result')
                else:
                    raise ValidationError('Local result cannot be set to a less critical level')
        res = super(BuildResult, self).write(values)
        if 'log_counter' in values:  # not 100% usefull but more correct ( see test_ir_logging)
            self.flush()
        return res

    def _add_child(self, param_values, orphan=False, description=False, additionnal_commit_links=False):
        if additionnal_commit_links:
            commit_link_ids = self.params_id.commit_link_ids
            commit_link_ids |= additionnal_commit_links
            param_values['commit_link_ids'] = commit_link_ids

        return self.create({
            'params_id': self.params_id.copy(param_values).id,
            'parent_id': self.id,
            'build_type': self.build_type,
            'description': description,
            'orphan_result': orphan,
            'keep_host': self.keep_host,
            'host': self.host if self.keep_host else False,
        })

    def result_multi(self):
        if all(build.global_result == 'ok' or not build.global_result for build in self):
            return 'ok'
        if any(build.global_result in ('skipped', 'killed', 'manually_killed') for build in self):
            return 'killed'
        if any(build.global_result == 'ko' for build in self):
            return 'ko'
        if any(build.global_result == 'warning' for build in self):
            return 'warning'
        return 'ko'  # ?

    def update_build_end(self):
        for build in self:
            build.build_end = now()
            if build.parent_id and build.parent_id.local_state in ('running', 'done'):
                build.parent_id.update_build_end()

    @api.depends('params_id.version_id.name')
    def _compute_dest(self):
        for build in self:
            if build.id:
                nickname = build.params_id.version_id.name
                nickname = re.sub(r'"|\'|~|\:', '', nickname)
                nickname = re.sub(r'_|/|\.', '-', nickname)
                build.dest = ("%05d-%s" % (build.id or 0, nickname[:32])).lower()

    @api.depends('port', 'dest', 'host')
    def _compute_domain(self):
        icp = self.env['ir.config_parameter'].sudo()
        nginx = icp.get_param('runbot.runbot_nginx', False)  # or just force nginx?
        domain = icp.get_param('runbot.runbot_domain', fqdn())
        for build in self:
            if nginx:
                build.domain = f"{build.dest}.{build.host}"
            else:
                build.domain = f"{domain}:{build.port}"

    @api.depends_context('batch')
    def _compute_build_url(self):
        batch = self.env.context.get('batch')
        for build in self:
            if batch:
                build.build_url = f"/runbot/batch/{batch.id}/build/{build.id}"
            else:
                build.build_url = f"/runbot/build/{build.id}"

    @api.depends('job_start', 'job_end')
    def _compute_job_time(self):
        """Return the time taken by the tests"""
        for build in self:
            if build.job_end and build.job_start:
                build.job_time = int(dt2time(build.job_end) - dt2time(build.job_start))
            elif build.job_start:
                build.job_time = int(time.time() - dt2time(build.job_start))
            else:
                build.job_time = 0

    @api.depends('build_start', 'build_end', 'global_state')
    def _compute_build_time(self):
        for build in self:
            if build.build_end and build.global_state != 'waiting':
                build.build_time = int(dt2time(build.build_end) - dt2time(build.build_start))
            elif build.build_start:
                build.build_time = int(time.time() - dt2time(build.build_start))
            else:
                build.build_time = 0

    @api.depends('job_start')
    def _compute_build_age(self):
        """Return the time between job start and now"""
        for build in self:
            if build.job_start:
                build.build_age = int(time.time() - dt2time(build.build_start))
            else:
                build.build_age = 0

    def _rebuild(self, message=None):
        """Force a rebuild and return a recordset of builds"""
        self.ensure_one()
        # TODO don't rebuild if there is a more recent build for this params?
        values = {
            'params_id': self.params_id.id,
            'build_type': 'rebuild',
        }
        if self.keep_host:
            values['host'] = self.host
            values['keep_host'] = True
        if self.parent_id:
            values |= {
                'parent_id': self.parent_id.id,
                'description': self.description,
            }
            self.orphan_result = True

        new_build = self.create(values)
        if self.parent_id:
            new_build._github_status()
        user = request.env.user if request else self.env.user
        new_build._log(
            'rebuild',
            f"Rebuild initiated by {user.name}{f' :{message}' if message else ''}",
        )

        if self.local_state != 'done':
            self._ask_kill(
                f'Killed by rebuild requested by {user.name} ({user.id}) (new build:{new_build.id})'
            )

        if not self.parent_id:
            slots = self.env['runbot.batch.slot'].search([('build_id', '=', self.id)])
            for slot in slots:
                slot.copy({
                    'build_id': new_build.id,
                    'link_type': 'rebuild',
                })
                slot.active = False
        return new_build

    def _skip(self, reason=None):
        """Mark builds ids as skipped"""
        if reason:
            self._logger('skip %s', reason)
        self.write({'local_state': 'done', 'local_result': 'skipped'})

    def _build_from_dest(self, dest):
        if dest_reg.match(dest):
            return self.browse(int(dest.split('-')[0]))
        return self.browse()

    def _filter_to_clean(self, dest_list, label):
        dest_by_builds_ids = defaultdict(list)
        ignored = set()
        icp = self.env['ir.config_parameter']
        hide_in_logs = icp.get_param('runbot.runbot_db_template', default='template0')
        full_gc_days = int(icp.get_param('runbot.full_gc_days', default=365))

        for dest in dest_list:
            if build := self._build_from_dest(dest):
                dest_by_builds_ids[build.id].append(dest)
            elif dest != hide_in_logs:
                ignored.add(dest)
        if ignored:
            _logger.info('%s (%s) not deleted because not dest format', label, list(ignored))
        builds = self.browse(dest_by_builds_ids)
        existing = builds.exists()
        if remaining := (builds - existing):
            dest_list = [dest for sublist in [dest_by_builds_ids[rem_id] for rem_id in remaining.ids] for dest in sublist]
            _logger.info('(%s) (%s) not deleted because no corresponding build found', label, " ".join(dest_list))
        for build in existing:
            if build.gc_date < fields.datetime.now():
                if build.local_state == 'done':
                    full = build.gc_date + datetime.timedelta(days=(full_gc_days)) < fields.datetime.now()
                    for db in dest_by_builds_ids[build.id]:
                        yield (db, full)
                elif build.local_state != 'running':
                    _logger.warning('db (%s) not deleted because state is not done', " ".join(dest_by_builds_ids[build.id]))

    def _local_cleanup(self, force=False, full=False):
        """
        Remove datadir and drop databases of build older than db_gc_days or db_gc_days_child.
        If force is set to True, does the same cleaning based on recordset without checking build age.
        """
        _logger.info('Local cleaning')
        _filter = self._filter_to_clean
        additionnal_conditions = []

        if force is True:
            def filter_ids(dest_list, label):
                for dest in dest_list:
                    build = self._build_from_dest(dest)
                    if build and build in self:
                        yield (dest, full)
                    elif not build:
                        _logger.info('%s (%s) skipped because not dest format', label, dest)
            _filter = filter_ids
            for _id in self.exists().ids:
                additionnal_conditions.append("datname like '%s-%%'" % _id)

        existing_db = list_local_dbs(additionnal_conditions=additionnal_conditions)

        for db, _ in _filter(dest_list=existing_db, label='db'):
            self._logger('Removing database')
            self._local_pg_dropdb(db)

        root = self.env['runbot.runbot']._root()
        builds_dir = os.path.join(root, 'build')

        if force is True:
            dests = [(build.dest, full) for build in self]
        else:
            dests = _filter(dest_list=os.listdir(builds_dir), label='workspace')

        for dest, full in dests:
            build_dir = os.path.join(builds_dir, dest)
            if full:
                _logger.info('Removing build dir "%s"', dest)
                shutil.rmtree(build_dir, ignore_errors=True)
                continue
            for f in os.listdir(build_dir):
                path = os.path.join(build_dir, f)
                if os.path.isdir(path) and f not in ('logs', 'tests'):
                    shutil.rmtree(path)
                elif f == 'logs':
                    log_path = os.path.join(build_dir, 'logs')
                    for f in os.listdir(log_path):
                        log_file_path = os.path.join(log_path, f)
                        if os.path.isdir(log_file_path):
                            shutil.rmtree(log_file_path)
                        elif f in ('run.txt', 'wake_up.txt') or not f.endswith('.txt'):
                            os.unlink(log_file_path)

    def _find_port(self):
        # currently used port
        ids = self.search([('local_state', 'not in', ['pending', 'done']), ('host', '=', fqdn())])
        ports = {i['port'] for i in ids.read(['port'])}

        # starting port
        icp = self.env['ir.config_parameter']
        port = int(icp.get_param('runbot.runbot_starting_port', default=2000))

        # find next free port
        while port in ports:
            port += 3
        return port

    def _logger(self, *l):
        l = list(l)
        for build in self:
            l[0] = f"{build.dest} {l[0]}"
            _logger.info(*l)

    def _get_docker_name(self):
        self.ensure_one()
        return f'{self.dest}_{self.active_step.name}'

    def _init_pendings(self, host):
        for build in self:
            if build.local_state != 'pending':
                raise UserError(f"Build {build.id} is not pending")
            if build.host != host.name:
                raise UserError(f"Build {build.id} does not have correct host")
            # allocate port and schedule first job
            values = {
                'port': self._find_port(),
                'job_start': now(),
                'build_start': now(),
                'job_end': False,
            }
            values |= build._next_job_values()
            build.write(values)
            if not build.active_step:
                build._log('_schedule', 'No job in config, doing nothing')
                build.local_result = 'warn'
                continue
            try:
                build._log(
                    '_schedule',
                    f'Init build environment with config {build.params_id.config_id.name} ',
                )
                os.makedirs(build._path('logs'), exist_ok=True)
            except Exception:
                _logger.exception('Failed initiating build %s', build.dest)
                build._log('_schedule', 'Failed initiating build')
                build._kill(result='ko')
                continue
            build._run_job()

    def _process_requested_actions(self):
        for build in self:
            if build.requested_action == 'deathrow':
                result = None
                if build.local_state != 'running' and build.global_result not in ('warn', 'ko'):
                    result = 'manually_killed'
                build._kill(result=result)
                continue

            if build.requested_action == 'wake_up':
                if docker_state(build._get_docker_name(), build._path()) == 'RUNNING':
                    build.write({'requested_action': False, 'local_state': 'running'})
                    build._log('wake_up', 'Waking up failed, **docker is already running**', log_type='markdown', level='SEPARATOR')
                elif not os.path.exists(build._path()):
                    build.write({'requested_action': False, 'local_state': 'done'})
                    build._log('wake_up', 'Impossible to wake-up, **build dir does not exists anymore**', log_type='markdown', level='SEPARATOR')
                else:
                    try:
                        log_path = build._path('logs', 'wake_up.txt')

                        port = self._find_port()
                        build.write({
                            'job_start': now(),
                            'job_end': False,
                            'active_step': False,
                            'requested_action': False,
                            'local_state': 'running',
                            'port': port,
                        })
                        build._log('wake_up', '**Waking up build**', log_type='markdown', level='SEPARATOR')
                        step_ids = build.params_id.config_id.step_ids()
                        if step_ids and step_ids[-1]._step_state() == 'running':
                            run_step = step_ids[-1]
                        else:
                            run_step = self.env.ref('runbot.runbot_build_config_step_run')
                        run_step._run_step(build, log_path, force=True)
                        # reload_nginx will be triggered by _run_run_odoo
                    except Exception:
                        _logger.exception('Failed to wake up build %s', build.dest)
                        build._log('_schedule', 'Failed waking up build', level='ERROR')
                        build.write({'requested_action': False, 'local_state': 'done'})
                continue

    def _schedule(self):
        """schedule the build"""
        icp = self.env['ir.config_parameter']
        for build in self:
            if build.local_state not in ['testing', 'running']:
                raise UserError(
                    f"Build {build.id} is not testing/running: {build.local_state}"
                )
            if (
                build.local_state == 'testing'
                and build.triggered_result
                and not build.active_step.ignore_triggered_result
            ):
                worst_result = self._get_worst_result([build.triggered_result, build.local_result])
                if worst_result != build.local_result:
                    build.local_result = build.triggered_result
                    build._github_status()  # failfast
            # check if current job is finished
            _docker_state = docker_state(build._get_docker_name(), build._path())
            if _docker_state == 'RUNNING':
                timeout = min(build.active_step.cpu_limit, int(icp.get_param('runbot.runbot_timeout', default=10000)))
                if build.local_state != 'running' and build.job_time > timeout:
                    build._log(
                        '_schedule',
                        f'{build.active_step.name if build.active_step else "?"} time exceeded ({build.job_time}s)',
                    )
                    build._kill(result='killed')
                continue
            elif _docker_state in ('UNKNOWN', 'GHOST') and (build.local_state == 'running' or build.active_step._is_docker_step()):  # todo replace with docker_start
                docker_time = time.time() - dt2time(build.docker_start or build.job_start)
                if docker_time < 5:
                    continue
                elif docker_time < 60:
                    _logger.info(
                        f'container "{build.job_time}" seems too take a while to start :{build._get_docker_name()}'
                    )
                    continue
                else:
                    build._log(
                        '_schedule',
                        f'Docker with state {_docker_state} not started after 60 seconds, skipping',
                        level='ERROR',
                    )
            # No job running, make result and select nex job
            build_values = {
                'job_end': now(),
                'docker_start': False,
            }
            # make result of previous job
            try:
                results = build.active_step._make_results(build)
            except Exception as e:
                if isinstance(e, RunbotException):
                    message = e.args[0][:300000]
                else:
                    message = 'An error occured while computing results of %s:\n %s' % (build.job, str(e).replace('\\n', '\n').replace("\\'", "'")[:10000])
                    _logger.exception(message)
                build._log('_make_results', message, level='ERROR')
                results = {'local_result': 'ko'}

            build_values |= results

            # compute statistics before starting next job
            build.active_step._make_stats(build)

            build.active_step.log_end(build)

            build_values |= build._next_job_values()

            ending_build = build.local_state not in ('done', 'running') and build_values.get('local_state') in ('done', 'running')
            if ending_build:
                build.update_build_end()

            build.write(build_values)
            if ending_build:
                if not build.local_result:  # Set 'ok' result if no result set (no tests job on build)
                    build.local_result = 'ok'
                    build._logger("No result set, setting ok by default")
                build._github_status()
            build._run_job()

    def _run_job(self):
        # run job
        for build in self:
            if build.local_state != 'done':
                build._logger('running %s', build.active_step.name)
                os.makedirs(build._path('logs'), exist_ok=True)
                os.makedirs(build._path('datadir'), exist_ok=True)
                try:
                    build.active_step._run(build)  # run should be on build?
                except Exception as e:
                    if isinstance(e, RunbotException):
                        message = e.args[0]
                    else:
                        message = '%s failed running step %s:\n %s' % (build.dest, build.job, str(e).replace('\\n', '\n').replace("\\'", "'"))
                    _logger.exception(message)
                    build._log("run", message, level='ERROR')
                    build._kill(result='ko')

    def _docker_run(self, **kwargs):
        self.ensure_one()
        if 'image_tag' not in kwargs:
            kwargs['image_tag'] = self.params_id.dockerfile_id.image_tag
        if kwargs['image_tag'] != 'odoo:DockerDefault':
            self._log('Preparing', f"Using Dockerfile Tag {kwargs['image_tag']}")
        containers_memory_limit = self.env['ir.config_parameter'].sudo().get_param('runbot.runbot_containers_memory', 0)
        if containers_memory_limit and 'memory' not in kwargs:
            kwargs['memory'] = int(float(containers_memory_limit) * 1024 ** 3)
        self.docker_start = now()
        if self.job_start:
            start_step_time = int(dt2time(self.docker_start) - dt2time(self.job_start))
            if start_step_time > 60:
                _logger.info('Step took %s seconds before starting docker', start_step_time)
        docker_run(**kwargs)

    def _path(self, *l, **kw):
        """Return the repo build path"""
        self.ensure_one()
        build = self
        root = self.env['runbot.runbot']._root()
        return os.path.join(root, 'build', build.dest, *l)

    def http_log_url(self):
        return f'http://{self.host}/runbot/static/build/{self.dest}/logs/'

    def _server(self, *path):
        """Return the absolute path to the direcory containing the server file, adding optional *path"""
        self.ensure_one()
        commit = self._get_server_commit()
        if os.path.exists(commit._source_path('odoo')):
            return commit._source_path('odoo', *path)
        return commit._source_path('openerp', *path)

    def _docker_source_folder(self, commit):
        return commit.repo_id.name

    def _checkout(self):
        self.ensure_one()  # will raise exception if hash not found, we don't want to fail for all build.
        # checkout branch
        start = time.time()
        exports = {}
        for commit in self.env.context.get('defined_commit_ids') or self.params_id.commit_ids:
            build_export_path = self._docker_source_folder(commit)
            if build_export_path in exports:
                self._log(
                    '_checkout',
                    f'Multiple repo have same export path in build, some source may be missing for {build_export_path}',
                    level='ERROR',
                )
                self._kill(result='ko')
            exports[build_export_path] = commit.export()

        checkout_time = time.time() - start
        if checkout_time > 60:
            self._log('checkout', f'Checkout took {int(checkout_time)} seconds')

        return exports

    def _get_available_modules(self):
        all_modules = {}
        available_modules = defaultdict(list)
        # repo_modules = []
        for commit in self.env.context.get('defined_commit_ids') or self.params_id.commit_ids:
            for (addons_path, module, manifest_file_name) in commit._get_available_modules():
                if module in all_modules:
                    self._log(
                        'Building environment',
                        f'{module} is a duplicated modules (found in "{commit._source_path(addons_path, module, manifest_file_name)}", already defined in {all_modules[module]._source_path(addons_path, module, manifest_file_name)})',
                        level='WARNING',
                    )
                else:
                    available_modules[commit.repo_id].append(module)
                    all_modules[module] = commit
        # return repo_modules, available_modules
        return available_modules

    def _get_modules_to_test(self, modules_patterns=''):
        self.ensure_one()

        def filter_patterns(patterns, default, all):
            default = set(default)
            patterns_list = (patterns or '').split(',')
            patterns_list = [p.strip() for p in patterns_list]
            for pat in patterns_list:
                if pat.startswith('-'):
                    pat = pat.strip('- ')
                    default -= {mod for mod in default if fnmatch.fnmatch(mod, pat)}
                elif pat:
                    default |= {mod for mod in all if fnmatch.fnmatch(mod, pat)}
            return default

        available_modules = []
        modules_to_install = set()
        for repo, module_list in self._get_available_modules().items():
            available_modules += module_list
            modules_to_install |= filter_patterns(repo.modules, module_list, module_list)

        modules_to_install = filter_patterns(self.params_id.modules, modules_to_install, available_modules)
        modules_to_install = filter_patterns(modules_patterns, modules_to_install, available_modules)

        return sorted(modules_to_install)

    def _local_pg_dropdb(self, dbname):
        with local_pgadmin_cursor() as local_cr:
            pid_col = 'pid' if local_cr.connection.server_version >= 90200 else 'procpid'
            query = f'SELECT pg_terminate_backend({pid_col}) FROM pg_stat_activity WHERE datname=%s'
            local_cr.execute(query, [dbname])
            local_cr.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        # cleanup filestore
        datadir = appdirs.user_data_dir()
        paths = [os.path.join(datadir, pn, 'filestore', dbname) for pn in 'OpenERP Odoo'.split()]
        cmd = ['rm', '-rf'] + paths
        _logger.info(' '.join(cmd))
        subprocess.call(cmd)

    def _local_pg_createdb(self, dbname):
        icp = self.env['ir.config_parameter']
        db_template = icp.get_param('runbot.runbot_db_template', default='template0')
        self._local_pg_dropdb(dbname)
        _logger.info("createdb %s", dbname)
        with local_pgadmin_cursor() as local_cr:
            local_cr.execute(sql.SQL("""CREATE DATABASE {} TEMPLATE %s LC_COLLATE 'C' ENCODING 'unicode'""").format(sql.Identifier(dbname)), (db_template,))
        self.env['runbot.database'].create({'name': dbname, 'build_id': self.id})

    def _log(self, func, message, level='INFO', log_type='runbot', path='runbot'):

        if len(message) > 300000:
            message = f'{message[:300000]}[Truncate, message too long]'

        self.ensure_one()
        _logger.info("Build %s %s %s", self.id, func, message)
        self.env['ir.logging'].create({
            'build_id': self.id,
            'level': level,
            'type': log_type,
            'name': 'odoo.runbot',
            'message': message,
            'path': path,
            'func': func,
            'line': '0',
        })

    def _kill(self, result=None):
        host = fqdn()
        for build in self:
            if build.host != host:
                continue
            build._log('kill', f'Kill build {build.dest}')
            docker_stop(build._get_docker_name(), build._path())
            v = {'local_state': 'done', 'requested_action': False, 'active_step': False, 'job_end': now()}
            if not build.build_end:
                v['build_end'] = now()
            if result:
                v['local_result'] = result
            build.write(v)
            self.env.cr.commit()
            build._github_status()
            self.invalidate_cache()

    def _ask_kill(self, lock=True, message=None):
        # if build remains in same bundle, it's ok like that
        # if build can be cross bundle, need to check number of ref to build
        if lock:
            self.env.cr.execute("""SELECT id FROM runbot_build WHERE parent_path like %s FOR UPDATE""", ['%s%%' % self.parent_path])
        self.ensure_one()
        user = request.env.user if request else self.env.user
        uid = user.id
        build = self
        message = (
            message
            or f'Killing build {build.dest}, requested by {user.name} (user #{uid})'
        )
        build._log('_ask_kill', message)
        if build.local_state == 'pending':
            build._skip()
        elif build.local_state in ['testing', 'running']:
            build.requested_action = 'deathrow'
        for child in build.children_ids:
            child._ask_kill(lock=False)

    def _wake_up(self):
        if self.local_state != 'done':
            self._log('wake_up', 'Impossibe to wake up, state is not done')
        else:
            self.requested_action = 'wake_up'

    def _get_server_commit(self):
        """
        returns a commit of the first repo containing server files found in commits or in build commits
        the commits param is not used in code base but could be usefull for jobs and crons
        """
        for commit in (self.env.context.get('defined_commit_ids') or self.params_id.commit_ids):
            if commit.repo_id.server_files:
                return commit
        raise ValidationError('No repo found with defined server_files')

    def _get_addons_path(self):
        for commit in (self.env.context.get('defined_commit_ids') or self.params_id.commit_ids):
            if not commit.repo_id.manifest_files:
                continue  # skip repo without addons
            source_path = self._docker_source_folder(commit)
            for addons_path in (commit.repo_id.addons_paths or '').split(','):
                if os.path.isdir(commit._source_path(addons_path)):
                    yield os.path.join(source_path, addons_path).strip(os.sep)

    def _get_server_info(self, commit=None):
        commit = commit or self._get_server_commit()
        for server_file in commit.repo_id.server_files.split(','):
            if os.path.isfile(commit._source_path(server_file)):
                return (commit, server_file)
        _logger.error('None of %s found in commit, actual commit content:\n %s' % (commit.repo_id.server_files, os.listdir(commit._source_path())))
        raise RunbotException(f'No server found in {commit.dname}')

    def _cmd(self, python_params=None, py_version=None, local_only=True, sub_command=None):
        """Return a list describing the command to start the build
        """
        self.ensure_one()
        build = self
        python_params = python_params or []
        py_version = py_version if py_version is not None else build._get_py_version()
        pres = []
        for commit_id in self.env.context.get('defined_commit_ids') or self.params_id.commit_ids:
            if not self.params_id.skip_requirements and os.path.isfile(commit_id._source_path('requirements.txt')):
                repo_dir = self._docker_source_folder(commit_id)
                requirement_path = os.path.join(repo_dir, 'requirements.txt')
                pres.append(
                    [
                        'sudo',
                        f'pip{py_version}',
                        'install',
                        '-r',
                        f'{requirement_path}',
                    ]
                )

        addons_paths = self._get_addons_path()
        (server_commit, server_file) = self._get_server_info()
        server_dir = self._docker_source_folder(server_commit)

        # commandline
        cmd = (
            [f'python{py_version}']
            + python_params
            + [os.path.join(server_dir, server_file)]
        )
        if sub_command:
            cmd += [sub_command]
        cmd += ['--addons-path', ",".join(addons_paths)]
        # options
        config_path = build._server("tools/config.py")
        if grep(config_path, "no-xmlrpcs"):  # move that to configs ?
            cmd.append("--no-xmlrpcs")
        if grep(config_path, "no-netrpc"):
            cmd.append("--no-netrpc")

        command = Command(pres, cmd, [], cmd_checker=build)

        # use the username of the runbot host to connect to the databases
        command.add_config_tuple('db_user', f'{pwd.getpwuid(os.getuid()).pw_name}')

        if local_only:
            if grep(config_path, "--http-interface"):
                command.add_config_tuple("http_interface", "127.0.0.1")
            elif grep(config_path, "--xmlrpc-interface"):
                command.add_config_tuple("xmlrpc_interface", "127.0.0.1")

        if grep(config_path, "log-db"):
            logdb_uri = self.env['ir.config_parameter'].get_param('runbot.runbot_logdb_uri')
            logdb = self.env.cr.dbname
            if logdb_uri and grep(build._server('sql_db.py'), 'allow_uri'):
                logdb = f'{logdb_uri}'
            command.add_config_tuple("log_db", f"{logdb}")
            if grep(config_path, 'log-db-level'):
                command.add_config_tuple("log_db_level", '25')

        if grep(config_path, "data-dir"):
            datadir = build._path('datadir')
            if not os.path.exists(datadir):
                os.mkdir(datadir)
            command.add_config_tuple("data_dir", '/data/build/datadir')

        return command

    def _cmd_check(self, cmd):
        """
        Check the cmd right before creating the build command line executed in
        a Docker container. If a database creation is found in the cmd, a
        'runbot.database' is created.
        This method is intended to be called from cmd itself
        """
        if '-d' in cmd:
            dbname = cmd[cmd.index('-d') + 1]
            self.env['runbot.database'].create({
                'name': dbname,
                'build_id': self.id
            })

    def _next_job_values(self):
        self.ensure_one()
        step_ids = self.params_id.config_id.step_ids()
        if not step_ids:  # no job to do, build is done
            return {'active_step': False, 'local_state': 'done'}

        if not self.active_step and self.local_state != 'pending':
            # means that a step has been run manually without using config
            return {'active_step': False, 'local_state': 'done'}

        if not self.active_step:
            next_index = 0
        else:
            if self.active_step not in step_ids:
                self._log('run', 'Config was modified and current step does not exists anymore, skipping.', level='ERROR')
                return {'active_step': False, 'local_state': 'done', 'local_result': self._get_worst_result([self.local_result, 'ko'])}
            next_index = step_ids.index(self.active_step) + 1

        while True:
            if next_index >= len(step_ids):  # final job, build is done
                return {'active_step': False, 'local_state': 'done'}
            new_step = step_ids[next_index]  # job to do, state is job_state (testing or running)
            if new_step.domain_filter and not self.filtered_domain(safe_eval(new_step.domain_filter)):

                self._log(
                    'run',
                    f'**Skipping** step ~~{new_step.name}~~ from config **{self.params_id.config_id.name}**',
                    log_type='markdown',
                    level='SEPARATOR',
                )
                next_index += 1
                continue
            break
        return {'active_step': new_step.id, 'local_state': new_step._step_state()}

    def _get_py_version(self):
        """return the python name to use from build batch"""
        (server_commit, server_file) = self._get_server_info()
        server_path = server_commit._source_path(server_file)
        with open(server_path, 'r') as f:
            if f.readline().strip().endswith('python3'):
                return '3'
        return ''

    def _parse_logs(self):
        """ Parse build logs to classify errors """
        BuildError = self.env['runbot.build.error']
        # only parse logs from builds in error and not already scanned
        builds_to_scan = self.search([('id', 'in', self.ids), ('local_result', '=', 'ko'), ('build_error_ids', '=', False)])
        ir_logs = self.env['ir.logging'].search([('level', '=', 'ERROR'), ('type', '=', 'server'), ('build_id', 'in', builds_to_scan.ids)])
        return BuildError._parse_logs(ir_logs)

    def is_file(self, file, mode='r'):
        file_path = self._path(file)
        return os.path.exists(file_path)

    def read_file(self, file, mode='r'):
        file_path = self._path(file)
        try:
            with open(file_path, mode) as f:
                return f.read()
        except Exception as e:
            self._log('readfile', f'exception: {e}')
            return False

    def write_file(self, file, data, mode='w'):
        file_path = self._path(file)
        file_dir = os.path.split(file_path)[0]
        os.makedirs(file_dir, exist_ok=True)
        try:
            with open(file_path, mode) as f:
                f.write(data)
        except Exception as e:
            self._log('write_file', f'exception: {e}')
            return False

    def make_dirs(self, dir_path):
        full_path = self._path(dir_path)
        try:
            os.makedirs(full_path, exist_ok=True)
        except Exception as e:
            self._log('make_dirs', f'exception: {e}')
            return False

    def build_type_label(self):
        self.ensure_one()
        return dict(self.fields_get('build_type', 'selection')['build_type']['selection']).get(self.build_type, self.build_type)

    def get_formated_job_time(self):
        return s2human(self.job_time)

    def get_formated_build_time(self):
        return s2human(self.build_time)

    def get_formated_build_age(self):
        return s2human(self.build_age)

    def get_color_class(self):

        if self.global_result == 'ko':
            return 'danger'
        if self.global_result == 'warn':
            return 'warning'

        if self.global_state == 'pending':
            return 'default'
        if self.global_state in ('testing', 'waiting'):
            return 'info'

        if self.global_result == 'ok':
            return 'success'

        if self.global_result in ('skipped', 'killed', 'manually_killed'):
            return 'killed'

    def _github_status(self, post_commit=True):
        """Notify github of failed/successful builds"""
        for build in self:
            # TODO maybe avoid to send status if build is killable (another new build exist and will send the status)
            if build.parent_id:
                if build.orphan_result:
                    _logger.info('Skipping result for orphan build %s', self.id)
                else:
                    build.parent_id._github_status(post_commit)
            elif build.params_id.config_id == build.params_id.trigger_id.config_id:
                if build.global_result in ('ko', 'warn'):
                    state = 'failure'
                elif build.global_state in ('pending', 'testing'):
                    state = 'pending'
                elif build.global_state in ('running', 'done'):
                    state = 'success' if build.global_result == 'ok' else 'error'
                else:
                    _logger.info("skipping github status for build %s ", build.id)
                    continue

                runbot_domain = self.env['runbot.runbot']._domain()
                trigger = self.params_id.trigger_id
                if trigger.ci_context:
                    target_url = (
                        trigger.ci_url
                        or f"http://{runbot_domain}/runbot/build/{build.id}"
                    )
                    desc = trigger.ci_description or f" (runtime {build.job_time}s)"
                    for build_commit in self.params_id.commit_link_ids:
                        commit = build_commit.commit_id
                        if 'base_' not in build_commit.match_type and commit.repo_id in trigger.repo_ids:
                            commit._github_status(build, trigger.ci_context, state, target_url, desc, post_commit)
