# -*- encoding: utf-8 -*-

from openerp import release
import logging

logger = logging.getLogger('upgrade')


def get_legacy_name(original_name, version):
    return f"legacy_{version.replace('.', '_')}_{original_name}"


def rename_columns(cr, column_spec, version):
    for table, renames in column_spec.iteritems():
        for old, new in renames:
            if new is None:
                new = get_legacy_name(old, version)
            logger.info("table %s, column %s: renaming to %s",
                        table, old, new)
            cr.execute(f'ALTER TABLE "{table}" RENAME "{old}" TO "{new}"')
            cr.execute(f'DROP INDEX IF EXISTS "{table}_{old}_index"')

column_renames = {
    'runbot_repo': [
        ('fallback_id', None)
    ]
}


def migrate(cr, version):
    if not version:
        return
    rename_columns(cr, column_renames, version)
