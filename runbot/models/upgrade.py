import re
from odoo import models, fields
from odoo.exceptions import UserError


class UpgradeExceptions(models.Model):
    _name = 'runbot.upgrade.exception'
    _description = 'Upgrade exception'

    active = fields.Boolean('Active', default=True)
    elements = fields.Text('Elements')
    bundle_id = fields.Many2one('runbot.bundle', index=True)
    info = fields.Text('Info')
    team_id = fields.Many2one('runbot.team', 'Assigned team', index=True)

    def _generate(self):
        if exceptions := self.search([]):
            return 'suppress_upgrade_warnings=%s' % (','.join(exceptions.mapped('elements'))).replace(' ', '').replace('\n', ',')
        return False


class UpgradeRegex(models.Model):
    _name = 'runbot.upgrade.regex'
    _description = 'Upgrade regex'

    active = fields.Boolean('Active', default=True)
    prefix = fields.Char('Type')
    regex = fields.Char('Regex')


class BuildResult(models.Model):
    _inherit = 'runbot.build'

    def _parse_upgrade_errors(self):
        ir_logs = self.env['ir.logging'].search([('level', 'in', ('ERROR', 'WARNING', 'CRITICAL')), ('type', '=', 'server'), ('build_id', 'in', self.ids)])

        upgrade_regexes = self.env['runbot.upgrade.regex'].search([])
        exception = []
        for log in ir_logs:
            for upgrade_regex in upgrade_regexes:
                if m := re.search(upgrade_regex.regex, log.message):
                    exception.append(f'{upgrade_regex.prefix}:{m.groups()[0]}')

        if not exception:
            raise UserError('Nothing found here')
        bundle = (
            batches[0].bundle_id.id
            if (batches := self.top_parent.slot_ids.mapped('batch_id'))
            else False
        )
        return {
            'name': 'Upgrade Exception',
            'type': 'ir.actions.act_window',
            'res_model': 'runbot.upgrade.exception',
            'view_mode': 'form',
            'context': {
                'default_elements': '\n'.join(exception),
                'default_bundle_id': bundle,
                'default_info': f'Automatically generated from build {self.id}',
            },
        }
