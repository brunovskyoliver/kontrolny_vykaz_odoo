{
    'name': 'Slovak Tax Control Statement',
    'version': '18.0.1.1.0',
    'category': 'Accounting/Localizations/Reporting',
    'license': 'LGPL-3',
    'summary': 'Slovak Tax Control Statement (Kontrolný výkaz DPH)',
    'description': """
Slovak Tax Control Statement
===========================
This module adds support for generating the Slovak Tax Control Statement (Kontrolný výkaz DPH).
    """,
    'author': 'Your Company',
    'depends': [
        'account',
        'l10n_sk',
        'mail',
    ],
    'data': [
        'views/kontrolny_vykaz_views.xml',
        'security/ir.model.access.csv',
        'data/sequence.xml',
        'views/menu_views.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
}