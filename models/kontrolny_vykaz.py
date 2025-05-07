from odoo import models, fields, api
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

class KontrolnyVykaz(models.Model):
    _name = 'kontrolny.vykaz'
    _description = 'VAT Control Statement'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char('Reference', required=True, readonly=True, default='/')
    company_id = fields.Many2one('res.company', string='Company', required=True, 
                               default=lambda self: self.env.company)
    date_from = fields.Date('Date From', required=True)
    date_to = fields.Date('Date To', required=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('generated', 'Generated'),
        ('confirmed', 'Confirmed'),
        ('exported', 'Exported')
    ], string='Status', default='draft', tracking=True)
    
    # A-section lines (customer invoices)
    a_section_line_ids = fields.One2many('kontrolny.vykaz.a.line', 'kontrolny_vykaz_id', 
                                        string='Section A - Customer Invoices')
    
    # Summary fields
    total_a_base = fields.Monetary(string='Total A Section Base', compute='_compute_totals', store=True)
    total_a_tax = fields.Monetary(string='Total A Section VAT', compute='_compute_totals', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id', readonly=True)
    
    # For month selection
    month = fields.Selection([
        ('01', 'January'),
        ('02', 'February'),
        ('03', 'March'),
        ('04', 'April'),
        ('05', 'May'),
        ('06', 'June'),
        ('07', 'July'),
        ('08', 'August'),
        ('09', 'September'),
        ('10', 'October'),
        ('11', 'November'),
        ('12', 'December')
    ], string='Month', required=True)
    year = fields.Integer(string='Year', required=True, default=lambda self: datetime.now().year)
    
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code('kontrolny.vykaz') or '/'
        return super().create(vals_list)
    
    @api.onchange('month', 'year')
    def _onchange_period(self):
        if self.month and self.year:
            date_from = datetime(int(self.year), int(self.month), 1).date()
            date_to = (date_from + relativedelta(months=1, days=-1))
            self.date_from = date_from
            self.date_to = date_to
    
    @api.depends('a_section_line_ids.base_amount', 'a_section_line_ids.tax_amount')
    def _compute_totals(self):
        for record in self:
            record.total_a_base = sum(record.a_section_line_ids.mapped('base_amount'))
            record.total_a_tax = sum(record.a_section_line_ids.mapped('tax_amount'))
    
    def action_generate_statement(self):
        self.ensure_one()
        self._unlink_existing_lines()
        self._generate_a_section_lines()
        self.state = 'generated'
        return True
    
    def _unlink_existing_lines(self):
        self.a_section_line_ids.unlink()
    
    def _generate_a_section_lines(self):
        """Generate lines for Section A (sales to VAT payers) and summarize individuals"""
        self.ensure_one()
        
        # Get all customer invoices for the period
        invoices = self.env['account.move'].search([
            ('company_id', '=', self.company_id.id),
            ('move_type', 'in', ['out_invoice', 'out_refund']),
            ('state', '=', 'posted'),
            ('invoice_date', '>=', self.date_from),
            ('invoice_date', '<=', self.date_to),
        ])
        
        # Storage for individuals summary (grouped by tax rate)
        individuals_tax_groups = {}
        
        # Process each invoice
        for invoice in invoices:
            # Check if partner is a Slovak VAT payer
            is_sk_vat_payer = invoice.partner_id.vat and invoice.partner_id.vat.upper().startswith('SK')
            
            # Group by tax rate
            tax_groups = {}
            for line in invoice.invoice_line_ids:
                if not line.tax_ids:
                    continue
                    
                # Process only lines with VAT taxes
                for tax in line.tax_ids:
                    if tax.amount not in tax_groups:
                        tax_groups[tax.amount] = {
                            'base': 0.0,
                            'tax': 0.0
                        }
                    
                    # Calculate base and tax amounts
                    price_subtotal = line.price_subtotal
                    tax_amount = line.price_total - line.price_subtotal
                    
                    # Add to group
                    tax_groups[tax.amount]['base'] += price_subtotal
                    tax_groups[tax.amount]['tax'] += tax_amount
            
            # Process the grouped data
            for tax_rate, amounts in tax_groups.items():
                if amounts['base'] == 0:
                    continue
                
                if is_sk_vat_payer:
                    # Create individual A section line for each Slovak VAT payer
                    self.env['kontrolny.vykaz.a.line'].create({
                        'kontrolny_vykaz_id': self.id,
                        'partner_id': invoice.partner_id.id,
                        'partner_vat': invoice.partner_id.vat,
                        'invoice_id': invoice.id,
                        'invoice_number': invoice.name,
                        'invoice_date': invoice.invoice_date,
                        'supply_date': invoice.taxable_supply_date,
                        'base_amount': amounts['base'],
                        'tax_rate': tax_rate,
                        'tax_amount': amounts['tax'],
                    })
                else:
                    # Add to individuals summary
                    if tax_rate not in individuals_tax_groups:
                        individuals_tax_groups[tax_rate] = {
                            'base': 0.0,
                            'tax': 0.0,
                            'count': 0
                        }
                    
                    individuals_tax_groups[tax_rate]['base'] += amounts['base']
                    individuals_tax_groups[tax_rate]['tax'] += amounts['tax']
                    individuals_tax_groups[tax_rate]['count'] += 1
        
        # Create summary lines for individuals
        for tax_rate, data in individuals_tax_groups.items():
            if data['base'] > 0:
                self.env['kontrolny.vykaz.a.line'].create({
                    'kontrolny_vykaz_id': self.id,
                    'partner_id': False,
                    'partner_vat': 'Individuals',
                    'invoice_id': False,
                    'invoice_number': f'Summary ({data["count"]} invoices)',
                    'invoice_date': self.date_to,
                    'supply_date': self.date_to,
                    'base_amount': data['base'],
                    'tax_rate': tax_rate,
                    'tax_amount': data['tax'],
                    'is_summary': True,
                })
    
    def action_confirm(self):
        self.ensure_one()
        self.state = 'confirmed'
        return True
    
    def action_export(self):
        self.ensure_one()
        self.state = 'exported'
        # Here you could add the export functionality for Slovak tax authorities
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Export Successful',
                'message': 'The Control Statement has been marked as exported.',
                'sticky': False,
            }
        }
    
    def action_reset_to_draft(self):
        self.ensure_one()
        self.state = 'draft'
        return True


class KontrolnyVykazALine(models.Model):
    _name = 'kontrolny.vykaz.a.line'
    _description = 'Control Statement Section A Line'
    
    kontrolny_vykaz_id = fields.Many2one('kontrolny.vykaz', string='Control Statement', ondelete='cascade')
    partner_id = fields.Many2one('res.partner', string='Customer')
    partner_vat = fields.Char(string='Customer VAT ID')
    invoice_id = fields.Many2one('account.move', string='Invoice')
    invoice_number = fields.Char(string='Invoice Number')
    invoice_date = fields.Date(string='Invoice Date')
    supply_date = fields.Date(string='Supply Date')
    base_amount = fields.Monetary(string='Base Amount')
    tax_rate = fields.Float(string='VAT Rate (%)')
    tax_amount = fields.Monetary(string='VAT Amount')
    currency_id = fields.Many2one(related='kontrolny_vykaz_id.currency_id')
    is_summary = fields.Boolean(string='Is Summary Line', default=False,
                             help='This is a summary line for individuals without VAT ID')