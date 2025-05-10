from odoo import models, fields, api
import base64
import xlsxwriter
from io import BytesIO
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
    
    # Excel export fields
    excel_file = fields.Binary('Excel File', readonly=True)
    excel_filename = fields.Char('Excel Filename', readonly=True)
    
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
    
    def action_export_excel(self):
        """Export KV data to Excel file matching the required format"""
        self.ensure_one()
        
        if self.state not in ['generated', 'confirmed', 'exported']:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Export Error',
                    'message': 'Please generate the Control Statement first.',
                    'type': 'warning',
                    'sticky': False,
                }
            }
        
        # Create Excel file in memory
        output = BytesIO()
        workbook = xlsxwriter.Workbook(output)
        worksheet = workbook.add_worksheet('KV DPHS')
        
        # Define formats
        header_format = workbook.add_format({
            'bold': True, 
            'align': 'center', 
            'valign': 'vcenter', 
            'bg_color': '#D3D3D3'
        })
        date_format = workbook.add_format({'num_format': 'mm/dd/yy'})
        number_format = workbook.add_format({'num_format': '#,##0.00'})
        
        # Write headers
        headers = [
            'ns1:IcDphPlatitela', 'ns1:Druh', 'ns1:Rok', 'ns1:Mesiac', 'ns1:Nazov', 
            'ns1:Stat', 'ns1:Obec', 'ns1:PSC', 'ns1:Ulica', 'ns1:Cislo', 'ns1:Tel', 'ns1:Email',
            'Odb', 'F', 'Den', 'Z', 'D', 'S', 'Odb2', 'FO', 'FP', 'ZR', 'DR', 'S3', 'Z4', 'D5', 'ZZn', 'DZn'
        ]
        
        for col, header in enumerate(headers):
            worksheet.write(0, col, header, header_format)
        
        # Get company data
        company = self.company_id
        
        # Process regular lines (with VAT ID)
        row = 1
        regular_lines = self.a_section_line_ids.filtered(lambda l: not l.is_summary)
        
        for line in regular_lines:
            # Skip if no partner VAT (should be handled in summary)
            if not line.partner_vat or not line.partner_vat.upper().startswith('SK'):
                continue
                
            # Format date as MM/DD/YY
            date_str = line.invoice_date.strftime('%m/%d/%y') if line.invoice_date else ''
            
            # Basic columns
            worksheet.write(row, 0, company.vat or '')  # ns1:IcDphPlatitela
            worksheet.write(row, 1, 'R')                # ns1:Druh (always R)
            worksheet.write(row, 2, self.year)          # ns1:Rok
            worksheet.write(row, 3, int(self.month))    # ns1:Mesiac
            worksheet.write(row, 4, company.name or '') # ns1:Nazov
            
            # Company address details
            worksheet.write(row, 5, company.country_id.name or 'Slovensko')  # ns1:Stat
            worksheet.write(row, 6, company.city or '')                      # ns1:Obec
            worksheet.write(row, 7, company.zip or '')                       # ns1:PSC
            worksheet.write(row, 8, company.street or '')                    # ns1:Ulica
            worksheet.write(row, 9, company.street2 or '')                   # ns1:Cislo
            worksheet.write(row, 10, company.phone or '')                    # ns1:Tel
            worksheet.write(row, 11, company.email or '')                    # ns1:Email
            
            # Invoice details
            worksheet.write(row, 12, line.partner_vat or '')                 # Odb (customer VAT)
            worksheet.write(row, 13, line.invoice_number or '')              # F (invoice number)
            worksheet.write(row, 14, date_str)                               # Den (date)
            worksheet.write(row, 15, line.base_amount, number_format)        # Z (base amount)
            worksheet.write(row, 16, line.tax_amount, number_format)         # D (tax amount)
            worksheet.write(row, 17, int(line.tax_rate))                     # S (tax rate)
            
            # Extra columns (mostly empty for regular entries)
            worksheet.write(row, 18, '')  # Odb2
            worksheet.write(row, 19, '')  # FO
            worksheet.write(row, 20, '')  # FP
            worksheet.write(row, 21, '')  # ZR
            worksheet.write(row, 22, '')  # DR
            worksheet.write(row, 23, '')  # S3
            worksheet.write(row, 24, self.total_a_base, number_format)  # Z4 (total base)
            worksheet.write(row, 25, self.total_a_tax, number_format)   # D5 (total tax)
            worksheet.write(row, 26, 0)   # ZZn
            worksheet.write(row, 27, 0)   # DZn
            
            row += 1
        
        # Process summary lines for individuals (no VAT ID) at the end
        summary_lines = self.a_section_line_ids.filtered(lambda l: l.is_summary)
        
        for line in summary_lines:
            # Format date as MM/DD/YY
            date_str = line.invoice_date.strftime('%m/%d/%y') if line.invoice_date else ''
            
            # Basic columns (same as regular lines)
            worksheet.write(row, 0, company.vat or '')  # ns1:IcDphPlatitela
            worksheet.write(row, 1, 'R')                # ns1:Druh (always R)
            worksheet.write(row, 2, self.year)          # ns1:Rok
            worksheet.write(row, 3, int(self.month))    # ns1:Mesiac
            worksheet.write(row, 4, company.name or '') # ns1:Nazov
            
            # Company address details (same as regular lines)
            worksheet.write(row, 5, company.country_id.name or 'Slovensko')  # ns1:Stat
            worksheet.write(row, 6, company.city or '')                      # ns1:Obec
            worksheet.write(row, 7, company.zip or '')                       # ns1:PSC
            worksheet.write(row, 8, company.street or '')                    # ns1:Ulica
            worksheet.write(row, 9, company.street2 or '')                   # ns1:Cislo
            worksheet.write(row, 10, company.phone or '')                    # ns1:Tel
            worksheet.write(row, 11, company.email or '')                    # ns1:Email
            
            # For individuals, the VAT ID (Odb) is blank or can be "Individuals"
            worksheet.write(row, 12, '')                                     # Odb (blank for individuals)
            worksheet.write(row, 13, line.invoice_number or '')              # F (summary invoice number)
            worksheet.write(row, 14, date_str)                               # Den (date)
            worksheet.write(row, 15, line.base_amount, number_format)        # Z (base amount)
            worksheet.write(row, 16, line.tax_amount, number_format)         # D (tax amount)
            worksheet.write(row, 17, int(line.tax_rate))                     # S (tax rate)
            
            # Extra columns (same as regular lines)
            worksheet.write(row, 18, '')  # Odb2
            worksheet.write(row, 19, '')  # FO
            worksheet.write(row, 20, '')  # FP
            worksheet.write(row, 21, '')  # ZR
            worksheet.write(row, 22, '')  # DR
            worksheet.write(row, 23, '')  # S3
            worksheet.write(row, 24, self.total_a_base, number_format)  # Z4 (total base)
            worksheet.write(row, 25, self.total_a_tax, number_format)   # D5 (total tax)
            worksheet.write(row, 26, 0)   # ZZn
            worksheet.write(row, 27, 0)   # DZn
            
            row += 1
        
        # Adjust column widths
        for col, header in enumerate(headers):
            worksheet.set_column(col, col, len(header) + 2)
        
        workbook.close()
        
        # Set excel file and filename
        filename = f'KV_DPHS_{self.year}_{self.month}.xlsx'
        file_data = base64.b64encode(output.getvalue())
        
        self.write({
            'excel_file': file_data,
            'excel_filename': filename,
            'state': 'exported' if self.state != 'exported' else self.state
        })
        
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content?model=kontrolny.vykaz&id={self.id}&field=excel_file&filename={filename}&download=true',
            'target': 'self',
        }


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