import enum
import itertools
import logging
import time

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)
class FreezeWizard(models.Model):
    _name = 'runbot_merge.project.freeze'
    _description = "Wizard for freezing a project('s master)"

    project_id = fields.Many2one('runbot_merge.project', required=True)
    branch_name = fields.Char(required=True, help="Name of the new branches to create")
    release_pr_ids = fields.One2many(
        'runbot_merge.project.freeze.prs', 'wizard_id',
        string="Release pull requests",
        help="Pull requests used as tips for the freeze branches, one per repository"
    )
    required_pr_ids = fields.Many2many(
        'runbot_merge.pull_requests', string="Required Pull Requests",
        domain="[('state', 'not in', ('closed', 'merged'))]",
        help="Pull requests which must have been merged before the freeze is allowed",
    )
    errors = fields.Text(compute='_compute_errors')

    _sql_constraints = [
        ('unique_per_project', 'unique (project_id)',
         "There should be only one ongoing freeze per project"),
    ]

    @api.depends('release_pr_ids.pr_id.label', 'required_pr_ids.state')
    def _compute_errors(self):
        errors = []
        if without := self.release_pr_ids.filtered(lambda p: not p.pr_id):
            errors.append(
                f"* Every repository must have a release PR, missing release PRs for {', '.join(p.repository_id.name for p in without)}."
            )

        labels = set(self.mapped('release_pr_ids.pr_id.label'))
        if len(labels) != 1:
            errors.append("* All release PRs must have the same label, found %r." % ', '.join(sorted(labels)))

        if unready := sum(
            p.state not in ('closed', 'merged') for p in self.required_pr_ids
        ):
            errors.append(f"* {unready} required PRs not ready.")

        self.errors = '\n'.join(errors) or False

    def action_cancel(self):
        self.project_id.check_access_rights('write')
        self.project_id.check_access_rule('write')
        self.sudo().unlink()

        return {'type': 'ir.actions.act_window_close'}

    def action_open(self):
        return {
            'type': 'ir.actions.act_window',
            'target': 'new',
            'name': f'Freeze project {self.project_id.name}',
            'view_mode': 'form',
            'res_model': self._name,
            'res_id': self.id,
        }

    def action_freeze(self):
        """ Attempts to perform the freeze.
        """
        # if there are still errors, reopen the wizard
        if self.errors:
            return self.action_open()

        project_id = self.project_id
        # need to create the new branch, but at the same time resequence
        # everything so the new branch is the second one, just after the branch
        # it "forks"
        master, rest = project_id.branch_ids[0], project_id.branch_ids[1:]
        seq = itertools.count(start=1) # start reseq at 1
        commands = [
            (1, master.id, {'sequence': next(seq)}),
            (0, 0, {
                'name': self.branch_name,
                'sequence': next(seq),
            })
        ]
        commands.extend((1, b.id, {'sequence': s}) for s, b in zip(seq, rest))
        project_id.branch_ids = commands

        # update release PRs to get merged on the newly created branch
        new_branch = project_id.branch_ids - master - rest
        self.release_pr_ids.mapped('pr_id').write({'target': new_branch.id, 'priority': 0})

        # create new branch on every repository
        errors = []
        repository = None
        for rel in self.release_pr_ids:
            repository = rel.repository_id
            gh = repository.github()
            # annoyance: can't directly alias a ref to an other ref, need to
            # resolve the "old" branch explicitely
            prev = gh('GET', f'git/refs/heads/{master.name}')
            if not prev.ok:
                errors.append(f"Unable to resolve branch {master.name} of repository {repository.name} to a commit.")
                break
            new_branch = gh(
                'POST',
                'git/refs',
                json={
                    'ref': f'refs/heads/{self.branch_name}',
                    'sha': prev.json()['object']['sha'],
                },
                check=False,
            )
            if not new_branch.ok:
                err = new_branch.json()['message']
                errors.append(f"Unable to create branch {master.name} of repository {repository.name}: {err}.")
                break
            time.sleep(1)

        # if an error occurred during creation, try to clean up then raise error
        if errors:
            for r in self.release_pr_ids:
                if r.repository_id == repository:
                    break

                deletion = r.repository_id.github().delete(f'git/refs/heads/{self.branch_name}')
                if not deletion.ok:
                    errors.append(f"Consequently unable to delete branch {self.branch_name} of repository {r.name}.")
                time.sleep(1)
            raise UserError('\n'.join(errors))

        # delete wizard
        self.sudo().unlink()
        # managed to create all the things, show reminder text (or close)
        if project_id.freeze_reminder:
            return {
                'type': 'ir.actions.act_window',
                'target': 'new',
                'name': f'Freeze reminder {project_id.name}',
                'view_mode': 'form',
                'res_model': project_id._name,
                'res_id': project_id.id,
                'view_id': self.env.ref('runbot_merge.project_freeze_reminder').id
            }

        return {'type': 'ir.actions.act_window_close'}

class ReleasePullRequest(models.Model):
    _name = 'runbot_merge.project.freeze.prs'
    _description = "links to pull requests used to \"cap\" freezes"

    wizard_id = fields.Many2one('runbot_merge.project.freeze', required=True, index=True, ondelete='cascade')
    repository_id = fields.Many2one('runbot_merge.repository', required=True)
    pr_id = fields.Many2one(
        'runbot_merge.pull_requests',
        domain='[("repository", "=", repository_id), ("state", "not in", ("closed", "merged"))]',
        string="Release Pull Request",
    )

    def write(self, vals):
        # only the pr should be writeable after initial creation
        assert 'wizard_id' not in vals
        assert 'repository_id' not in vals
        # and if the PR gets set, it should match the requested repository
        if 'pr_id' in vals:
            assert self.env['runbot_merge.pull_requests'].browse(vals['pr_id'])\
                       .repository == self.repository_id

        return super().write(vals)

class RepositoryFreeze(models.Model):
    _inherit = 'runbot_merge.repository'
    freeze = fields.Boolean(required=True, default=True,
                            help="Freeze this repository by default")

@enum.unique
class Colors(enum.IntEnum):
    No = 0
    Red = 1
    Orange = 2
    Yellow = 3
    LightBlue = 4
    DarkPurple = 5
    Salmon = 6
    MediumBlue = 7
    DarkBlue = 8
    Fuchsia = 9
    Green = 10
    Purple = 11

STATE_COLORMAP = {
    'opened': Colors.No,
    'closed': Colors.Orange,
    'validated': Colors.No,
    'approved': Colors.No,
    'ready': Colors.LightBlue,
    'merged': Colors.Green,
    'error': Colors.Red,
}
class PullRequestColor(models.Model):
    _inherit = 'runbot_merge.pull_requests'

    state_color = fields.Integer(compute='_compute_state_color')

    @api.depends('state')
    def _compute_state_color(self):
        for p in self:
            p.state_color = STATE_COLORMAP[p.state]
