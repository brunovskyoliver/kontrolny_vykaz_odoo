# Slovak VAT Control Statement (Kontrolný výkaz DPH)

This module adds support for generating the Slovak Tax Control Statement (Kontrolný výkaz DPH).

## Features

1. Generates XML exports matching Slovak tax authority requirements
2. Generates Excel exports for convenience
3. Follows Slovak legal terminology and export format rules
4. Handles VAT payers vs. non-VAT payers appropriately using the `x_platca_dph` field
5. Filters invoices by taxable supply date
6. Properly processes regular invoices (A1 section) and credit notes/dobropisy (C1 section)
7. Correctly handles total calculations (D2 section) with negative amounts from refunds

## Version History

### 18.0.1.1.0
- Fixed issue with refunds not being found in search
- Enhanced search to include refunds without taxable_supply_date set
- Added database migration script to add is_refund column
- Improved logging of invoice counts by type

### 18.0.1.0.0
- Initial release

## Testing the Fix for Refunds

To test the fix for refunds:

1. Upgrade the module to apply the migration
2. Create a new control statement for a period with refunds
3. Check that refunds are properly included in the statement
4. Verify that refunds are exported as C1 records in the XML
5. Check that totals in section D2 correctly account for refund amounts

## Technical Notes

The module now uses multiple queries to find all relevant invoices:
- Regular invoices with taxable_supply_date in period
- Refunds (out_refund) with taxable_supply_date in period
- Refunds (out_refund) with invoice_date in period when taxable_supply_date is not set
- Reversed invoices (payment_state = 'reversed') with taxable_supply_date or invoice_date in period
