from odoo import api, SUPERUSER_ID

def migrate(cr, version):
    """
    Add is_refund column to kontrolny_vykaz_a_line table if it doesn't exist.
    """
    # Check if the column exists
    cr.execute("SELECT column_name FROM information_schema.columns WHERE table_name='kontrolny_vykaz_a_line' AND column_name='is_refund'")
    if not cr.fetchone():
        # Add the is_refund column with default value False
        cr.execute("ALTER TABLE kontrolny_vykaz_a_line ADD COLUMN is_refund boolean DEFAULT FALSE")
        
        # Update environment
        env = api.Environment(cr, SUPERUSER_ID, {})
        
        # Get all records that are refunds based on related invoice type
        cr.execute("""
            UPDATE kontrolny_vykaz_a_line kvl
            SET is_refund = TRUE
            FROM account_move am
            WHERE kvl.invoice_id = am.id
            AND (am.move_type = 'out_refund' OR am.payment_state = 'reversed')
        """)
        
        # Log the migration
        env['ir.logging'].create({
            'name': 'kontrolny_vykaz_migration',
            'type': 'server',
            'dbname': cr.dbname,
            'level': 'info',
            'message': 'Added is_refund column to kontrolny_vykaz_a_line table and populated values',
            'path': 'addons/kontrolny_vykaz/migrations/18.0.1.1.0/post-migrate.py',
            'func': 'migrate',
            'line': 23
        })
