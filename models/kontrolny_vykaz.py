from odoo import models, fields, api
import base64
import xlsxwriter
from io import BytesIO
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import xml.etree.ElementTree as ET
from xml.dom import minidom

import logging
_logger = logging.getLogger(__name__)



class KontrolnyVykaz(models.Model):
    _name = 'kontrolny.vykaz'
    _description = 'Kontrolný výkaz DPH'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char('Referencia', required=True, readonly=True, default='/')
    company_id = fields.Many2one('res.company', string='Spoločnosť', required=True, 
                               default=lambda self: self.env.company)
    date_from = fields.Date('Dátum od', required=True)
    date_to = fields.Date('Dátum do', required=True)
    state = fields.Selection([
        ('draft', 'Koncept'),
        ('generated', 'Vygenerované'),
        ('confirmed', 'Potvrdené'),
        ('exported', 'Exportované')
    ], string='Stav', default='draft', tracking=True)
    
    # A-section lines (customer invoices)
    a_section_line_ids = fields.One2many('kontrolny.vykaz.a.line', 'kontrolny_vykaz_id', 
                                        string='Oddiel A - Faktúry pre odberateľov')
    
    # Summary fields
    total_a_base = fields.Monetary(string='Základ dane oddiel A', compute='_compute_totals', store=True)
    total_a_tax = fields.Monetary(string='DPH oddiel A', compute='_compute_totals', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id', readonly=True)
    
    # For month selection
    month = fields.Selection([
        ('01', 'Január'),
        ('02', 'Február'),
        ('03', 'Marec'),
        ('04', 'Apríl'),
        ('05', 'Máj'),
        ('06', 'Jún'),
        ('07', 'Júl'),
        ('08', 'August'),
        ('09', 'September'),
        ('10', 'Október'),
        ('11', 'November'),
        ('12', 'December')
    ], string='Mesiac', required=True)
    year = fields.Integer(string='Rok', required=True, default=lambda self: datetime.now().year)
    
    # Excel export fields
    excel_file = fields.Binary('Excel súbor', readonly=True)
    excel_filename = fields.Char('Názov Excel súboru', readonly=True)
    
    # XML export fields 
    xml_file = fields.Binary('XML súbor', readonly=True)
    xml_filename = fields.Char('Názov XML súboru', readonly=True)
    
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
        
        # Get all customer invoices for the period based on taxable supply date
        invoices = self.env['account.move'].search([
            ('company_id', '=', self.company_id.id),
            ('move_type', 'in', ['out_invoice', 'out_refund']),
            ('state', '=', 'posted'),
            ('taxable_supply_date', '>=', self.date_from),
            ('taxable_supply_date', '<=', self.date_to),
        ])
        
        # Storage for individuals summary (grouped by tax rate)
        individuals_tax_groups = {}
        
        # Process each invoice
        for invoice in invoices:
            # Check if the partner has a Slovak VAT ID - that's the ONLY condition for going into separate A1 records
            # The x_platca_dph field only affects whether the VAT ID is shown in the XML/Excel
            has_vat_id = invoice.partner_id.vat and invoice.partner_id.vat.upper().startswith('SK')
            
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
                
                if has_vat_id:
                    # Create individual A section line for each entity with Slovak VAT ID
                    # regardless of x_platca_dph status
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
                    'invoice_number': f'Súhrn ({data["count"]} faktúr)',
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
        """Export KV data to XML file matching the required format for Slovak tax authorities"""
        self.ensure_one()
        
        if self.state not in ['confirmed', 'exported']:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Chyba exportu',
                    'message': 'Prosím, najprv potvrďte kontrolný výkaz.',
                    'type': 'warning',
                    'sticky': False,
                }
            }
            
        # Create XML structure
        # Define the namespace
        xmlns = "https://ekr.financnasprava.sk/Formulare/XSD/kv_dph_2025.xsd"
        
        # Root element
        root = ET.Element("KVDPH_2025", xmlns=xmlns)
        
        # Identification section
        identification = ET.SubElement(root, "Identifikacia")
        
        # Get company data
        company = self.company_id
        vat_number = company.vat or ''
        if vat_number and not vat_number.startswith('SK'):
            vat_number = 'SK' + vat_number.replace('SK', '')
            
        # Add identification details
        ET.SubElement(identification, "IcDphPlatitela").text = vat_number
        ET.SubElement(identification, "Druh").text = "R"  # Regular statement
        
        # Period information
        period = ET.SubElement(identification, "Obdobie")
        ET.SubElement(period, "Rok").text = str(self.year)
        ET.SubElement(period, "Mesiac").text = str(int(self.month))
        
        # Company details
        ET.SubElement(identification, "Nazov").text = company.name or ''
        ET.SubElement(identification, "Stat").text = company.country_id.name or 'Slovensko'
        ET.SubElement(identification, "Obec").text = company.city or ''
        ET.SubElement(identification, "PSC").text = company.zip or ''
        ET.SubElement(identification, "Ulica").text = company.street or ''
        ET.SubElement(identification, "Cislo").text = company.street2 or ''
        ET.SubElement(identification, "Tel").text = company.phone or ''
        ET.SubElement(identification, "Email").text = company.email or ''
        
        # Transactions section
        transactions = ET.SubElement(root, "Transakcie")
        
        # Process regular Section A lines (A1 transactions - sales with VAT)
        # Only include lines with VAT-registered customers (exclude summary lines for individuals)
        for line in self.a_section_line_ids.filtered(lambda l: not l.is_summary):
            if line.base_amount <= 0:
                continue
                
            # Format date as YYYY-MM-DD
            date_str = line.supply_date.strftime('%Y-%m-%d') if line.supply_date else ''
            
            # Create A1 element with attributes
            a1 = ET.SubElement(transactions, "A1")
            
            # For VAT-registered customers, check if they're marked as VAT payers (x_platca_dph)
            if line.partner_id and hasattr(line.partner_id, 'x_platca_dph') and line.partner_id.x_platca_dph and line.partner_vat and line.partner_vat.upper().startswith('SK'):
                a1.set("Odb", line.partner_vat)
            else:
                a1.set("Odb", "")  # Empty for non-VAT customers or when x_platca_dph is False
                
            a1.set("F", line.invoice_number or '')
            a1.set("Den", date_str)
            a1.set("Z", "{:.2f}".format(line.base_amount))
            a1.set("D", "{:.2f}".format(line.tax_amount))
            a1.set("S", str(int(line.tax_rate)))
        
        # If we had credit notes (C1 transactions), we would add them here
        # For example:
        # c1 = ET.SubElement(transactions, "C1")
        # c1.set("Odb", vat_number)
        # c1.set("FO", "reference_number")
        # ...
        
        # Add totals section (D2) - This includes all transactions including those for individuals
        # The total amounts include both regular A1 lines and summary lines (individuals without VAT ID)
        d2 = ET.SubElement(transactions, "D2")
        d2.set("Z", "{:.2f}".format(self.total_a_base))
        d2.set("D", "{:.2f}".format(self.total_a_tax))
        d2.set("ZZn", "0.00")
        d2.set("DZn", "0.00")
        
        # Convert to properly formatted XML string
        rough_string = ET.tostring(root, 'utf-8')
        
        # Add XML declaration
        xml_declaration = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        
        # Parse the string to get a DOM representation
        reparsed = minidom.parseString(rough_string)
        pretty_xml = reparsed.toprettyxml(indent="  ")
        
        # Remove extra blank lines that minidom sometimes adds
        xml_lines = [line for line in pretty_xml.splitlines() if line.strip()]
        pretty_xml = '\n'.join(xml_lines)
        
        # Replace the XML declaration with our custom one
        if pretty_xml.startswith('<?xml'):
            pretty_xml = xml_declaration + pretty_xml.split('\n', 1)[1]
        else:
            pretty_xml = xml_declaration + pretty_xml
        
        # Set xml file and filename
        filename = f'KVDPH_{self.year}_MESIAC_{int(self.month)}.XML'
        file_data = base64.b64encode(pretty_xml.encode('utf-8'))
        
        self.write({
            'xml_file': file_data,
            'xml_filename': filename,
            'state': 'exported'
        })
        
        # Log the export with a note about individuals
        total_vat_registered = len(self.a_section_line_ids.filtered(lambda l: not l.is_summary))
        total_individuals = len(self.a_section_line_ids.filtered(lambda l: l.is_summary))
        total_with_vat_id = len(self.a_section_line_ids.filtered(lambda l: not l.is_summary and l.partner_vat and l.partner_vat.upper().startswith('SK')))
        total_with_empty_odb = len(self.a_section_line_ids.filtered(lambda l: not l.is_summary and l.partner_vat and l.partner_vat.upper().startswith('SK') and ((hasattr(l.partner_id, 'x_platca_dph') and not l.partner_id.x_platca_dph) or not hasattr(l.partner_id, 'x_platca_dph'))))
        
        _logger.info(
            f"Exported XML file: {filename} with {total_vat_registered} A1 records. "
            f"{total_individuals} summary records for individuals were included in totals but not as A1 records. "
            f"{total_with_vat_id} records have a Slovak VAT ID, of which {total_with_empty_odb} have x_platca_dph=False (empty Odb attribute)."
        )
        
        # Display a message about individuals being excluded from A1 records
        total_vat_registered = len(self.a_section_line_ids.filtered(lambda l: not l.is_summary))
        total_individuals = len(self.a_section_line_ids.filtered(lambda l: l.is_summary))
        total_with_vat_id = len(self.a_section_line_ids.filtered(lambda l: not l.is_summary and l.partner_vat and l.partner_vat.upper().startswith('SK')))
        total_with_empty_odb = len(self.a_section_line_ids.filtered(lambda l: not l.is_summary and l.partner_vat and l.partner_vat.upper().startswith('SK') and ((hasattr(l.partner_id, 'x_platca_dph') and not l.partner_id.x_platca_dph) or not hasattr(l.partner_id, 'x_platca_dph'))))
        
        message = f"""
            <p>Kontrolný výkaz bol úspešne exportovaný a stiahnutý ako XML súbor.</p>
            <ul>
                <li><strong>XML Súbor:</strong> {filename}</li>
                <li><strong>Počet A1 záznamov:</strong> {total_vat_registered}</li>
                <li><strong>Počet súhrnných záznamov pre fyzické osoby:</strong> {total_individuals}</li>
                <li><strong>Počet záznamov s SK IČ DPH:</strong> {total_with_vat_id}</li>
                <li><strong>Počet záznamov s prázdnym atribútom Odb (x_platca_dph=False):</strong> {total_with_empty_odb}</li>
            </ul>
            <p><em>Poznámka: Súhrnné záznamy pre fyzické osoby bez IČ DPH sú zahrnuté v celkových sumách, ale nie sú exportované ako samostatné A1 záznamy v XML súbore.</em></p>
            <p><em>Upozornenie: Partneri s IČ DPH SK, ktorí majú nastavené x_platca_dph=False, majú prázdny atribút Odb v A1 záznamoch.</em></p>
        """
        
        self.message_post(body=message)
        
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content?model=kontrolny.vykaz&id={self.id}&field=xml_file&filename={filename}&download=true',
            'target': 'self',
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
                    'title': 'Chyba exportu',
                    'message': 'Prosím, najprv vygenerujte kontrolný výkaz.',
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
            # We leave in entries where partner has VAT but x_platca_dph is False (will have empty Odb)
            if not line.partner_vat or not line.partner_vat.upper().startswith('SK'):
                continue
                
            # Format date as MM/DD/YY
            date_str = line.supply_date.strftime('%m/%d/%y') if line.supply_date else ''
            
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
            if line.partner_id and hasattr(line.partner_id, 'x_platca_dph') and line.partner_id.x_platca_dph and line.partner_vat and line.partner_vat.upper().startswith('SK'):
                worksheet.write(row, 12, line.partner_vat or '')                 # Odb (customer VAT)
            else:
                worksheet.write(row, 12, '')                                     # Odb (empty for non-VAT payers)
                
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
            date_str = line.supply_date.strftime('%m/%d/%y') if line.supply_date else ''
            
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
        filename = f'KV_DPHS_{self.year}_{int(self.month)}.xlsx'
        file_data = base64.b64encode(output.getvalue())
        
        self.write({
            'excel_file': file_data,
            'excel_filename': filename,
            'state': 'exported' if self.state != 'exported' else self.state
        })
        
        # Log success message for the Excel export
        total_vat_registered = len(self.a_section_line_ids.filtered(lambda l: not l.is_summary))
        total_individuals = len(self.a_section_line_ids.filtered(lambda l: l.is_summary))
        total_with_vat_id = len(self.a_section_line_ids.filtered(lambda l: not l.is_summary and l.partner_vat and l.partner_vat.upper().startswith('SK')))
        total_with_empty_odb = len(self.a_section_line_ids.filtered(lambda l: not l.is_summary and l.partner_vat and l.partner_vat.upper().startswith('SK') and ((hasattr(l.partner_id, 'x_platca_dph') and not l.partner_id.x_platca_dph) or not hasattr(l.partner_id, 'x_platca_dph'))))
        
        self.message_post(body=f"""
            <p>Kontrolný výkaz bol úspešne exportovaný do Excel súboru.</p>
            <ul>
                <li><strong>Excel Súbor:</strong> {filename}</li>
                <li><strong>Počet A1 záznamov:</strong> {total_vat_registered}</li>
                <li><strong>Počet súhrnných záznamov pre fyzické osoby:</strong> {total_individuals}</li>
                <li><strong>Počet záznamov s SK IČ DPH:</strong> {total_with_vat_id}</li>
                <li><strong>Počet záznamov s prázdnym atribútom Odb (x_platca_dph=False):</strong> {total_with_empty_odb}</li>
            </ul>
            <p><em>Poznámka: Aj v Excel súbore sa používa pole x_platca_dph na určenie, či sa má v stĺpci Odb zobraziť IČ DPH.</em></p>
        """)
        
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content?model=kontrolny.vykaz&id={self.id}&field=excel_file&filename={filename}&download=true',
            'target': 'self',
        }


class KontrolnyVykazALine(models.Model):
    _name = 'kontrolny.vykaz.a.line'
    _description = 'Riadok oddielu A kontrolného výkazu'
    
    kontrolny_vykaz_id = fields.Many2one('kontrolny.vykaz', string='Kontrolný výkaz', ondelete='cascade')
    partner_id = fields.Many2one('res.partner', string='Odberateľ')
    partner_vat = fields.Char(string='IČ DPH odberateľa')
    invoice_id = fields.Many2one('account.move', string='Faktúra')
    invoice_number = fields.Char(string='Číslo faktúry')
    invoice_date = fields.Date(string='Dátum vyhotovenia')
    supply_date = fields.Date(string='Dátum dodania')
    base_amount = fields.Monetary(string='Základ dane')
    tax_rate = fields.Float(string='Sadzba DPH (%)')
    tax_amount = fields.Monetary(string='Suma DPH')
    currency_id = fields.Many2one(related='kontrolny_vykaz_id.currency_id')
    is_summary = fields.Boolean(string='Je súhrnný riadok', default=False,
                             help='Toto je súhrnný riadok pre fyzické osoby bez IČ DPH')