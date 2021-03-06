# -*- coding: utf-8 -*-
##############################################################################
#
#    Copyright (C) 2014-2018 Compassion CH (http://www.compassion.ch)
#    Releasing children from poverty in Jesus' name
#    @author: Emanuel Cino <ecino@compassion.ch>
#
#    The licence is in the file __manifest__.py
#
##############################################################################

import logging

from odoo import api, models
from odoo.exceptions import UserError
from odoo.tools import mod10r

logger = logging.getLogger(__name__)


class BankStatementLine(models.Model):

    _inherit = 'account.bank.statement.line'

    ##########################################################################
    #                             PUBLIC METHODS                             #
    ##########################################################################
    def get_statement_line_for_reconciliation_widget(self):
        # Add partner reference for reconcile view
        res = super(BankStatementLine,
                    self).get_statement_line_for_reconciliation_widget()
        res['partner_ref'] = self.partner_id.ref
        return res

    ##########################################################################
    #                             PRIVATE METHODS                            #
    ##########################################################################
    @api.multi
    def process_reconciliation(self, counterpart_aml_dicts=None,
                               payment_aml_rec=None, new_aml_dicts=None):
        """ Create invoice if product_id is set in move_lines
        to be created. """
        self.ensure_one()
        partner_invoices = dict()
        partner_inv_data = dict()
        old_counterparts = dict()
        if counterpart_aml_dicts is None:
            counterpart_aml_dicts = list()
        if new_aml_dicts is None:
            new_aml_dicts = list()
        partner_id = self.partner_id.id
        counterparts = [data['move_line'] for data in counterpart_aml_dicts]
        counterparts = reduce(lambda m1, m2: m1 + m2.filtered('invoice_id'),
                              counterparts, self.env['account.move.line'])
        index = 0
        for mv_line_dict in new_aml_dicts:
            # Add partner_id if missing from mvl_data
            mv_line_dict['partner_id'] = partner_id
            if mv_line_dict.get('product_id'):
                # Create invoice
                if partner_id in partner_inv_data:
                    partner_inv_data[partner_id].append(mv_line_dict)
                else:
                    partner_inv_data[partner_id] = [mv_line_dict]
                mv_line_dict['index'] = index

            index += 1
            if counterparts:
                # An invoice exists for that partner, we will use it
                # to put leftover amount in it, if any exists.
                invoice = counterparts[0].invoice_id
                partner_invoices[partner_id] = invoice
                old_counterparts[invoice.id] = counterparts[0]

        # Create invoice and update move_line_dicts to reconcile them.
        nb_new_aml_removed = 0
        for partner_id, partner_data in partner_inv_data.iteritems():
            invoice = partner_invoices.get(partner_id)
            new_counterpart = self._create_invoice_from_mv_lines(
                partner_data, invoice)
            if invoice:
                # Remove new move lines
                for data in partner_data:
                    index = data.pop('index') - nb_new_aml_removed
                    del new_aml_dicts[index]
                    nb_new_aml_removed += 1

                # Update old counterpart
                for counterpart_data in counterpart_aml_dicts:
                    if counterpart_data['move_line'] == \
                            old_counterparts[invoice.id]:
                        counterpart_data['move_line'] = new_counterpart
                        counterpart_data['credit'] = new_counterpart.debit
                        counterpart_data['debit'] = new_counterpart.credit
            else:
                # Add new counterpart and remove new move line
                for data in partner_data:
                    index = data.pop('index') - nb_new_aml_removed
                    del new_aml_dicts[index]
                    nb_new_aml_removed += 1
                    data['move_line'] = new_counterpart
                    counterpart_aml_dicts.append(data)

        return super(BankStatementLine, self).process_reconciliation(
            counterpart_aml_dicts, payment_aml_rec, new_aml_dicts)

    def _create_invoice_from_mv_lines(self, mv_line_dicts, invoice=None):
        # Generate a unique bvr_reference
        if self.ref and len(self.ref) == 27:
            ref = self.ref
        elif self.ref and len(self.ref) > 27:
            ref = mod10r(self.ref[:26])
        else:
            ref = mod10r((self.date.replace('-', '') + str(
                self.statement_id.id) + str(self.id)).ljust(26, '0'))

        if invoice:
            invoice.action_invoice_cancel()
            invoice.action_invoice_draft()
            invoice.env.invalidate_all()
            invoice.write({'origin': self.statement_id.name})

        else:
            # Lookup for an existing open invoice matching the criterias
            invoices = self._find_open_invoice(mv_line_dicts)
            if invoices:
                # Get the bvr reference of the invoice or set it
                invoice = invoices[0]
                invoice.write({'origin': self.statement_id.name})
                if invoice.reference and not self.ref:
                    ref = invoice.reference
                else:
                    invoice.write({'reference': ref})
                self.write({
                    'ref': ref,
                    'invoice_id': invoice.id})
                return True

            # Setup a new invoice if no existing invoice is found
            inv_data = self._get_invoice_data(ref, mv_line_dicts)
            invoice = self.env['account.invoice'].create(inv_data)

        for mv_line_dict in mv_line_dicts:
            inv_line_data = self._get_invoice_line_data(mv_line_dict, invoice)
            self.env['account.invoice.line'].create(inv_line_data)

        invoice.action_invoice_open()
        self.ref = ref

        # Update move_lines data
        counterpart = invoice.move_id.line_ids.filtered(
            lambda ml: ml.debit > 0)
        return counterpart

    def _get_invoice_data(self, ref, mv_line_dicts):
        """
        Sets the invoice
        :param ref: reference of the statement line
        :param mv_line_dicts: all data for reconciliation
        :return: dict of account.invoice vals
        """
        journal_id = self.env['account.journal'].search(
            [('type', '=', 'sale')], limit=1).id
        return {
            'account_id':
                self.partner_id.property_account_receivable_id.id,
            'type': 'out_invoice',
            'partner_id': self.partner_id.id,
            'journal_id': journal_id,
            'date_invoice': self.date,
            'reference': ref,
            'origin': self.statement_id.name,
            'comment': ';'.join(map(
                lambda d: d.get('comment', ''),
                mv_line_dicts)),
            'currency_id': self.journal_currency_id.id
        }

    def _get_invoice_line_data(self, mv_line_dict, invoice):
        """
        Setup invoice line data
        :param mv_line_dict: values from the move_line reconciliation
        :param invoice: destination invoice
        :return: dict of account.invoice.line vals
        """
        amount = mv_line_dict['credit']
        account_id = mv_line_dict['account_id']
        invl_vals = {
            'name': self.name,
            'account_id': account_id,
            'price_unit': amount,
            'price_subtotal': amount,
            'user_id': mv_line_dict.get('user_id'),
            'quantity': 1,
            'product_id': mv_line_dict['product_id'],
            'invoice_id': invoice.id,
        }
        # Remove analytic account from bank journal item:
        # it is only useful in the invoice journal item
        analytic = mv_line_dict.pop('analytic_account_id', False)
        if analytic:
            invl_vals['account_analytic_id'] = analytic
        return invl_vals

    def _find_open_invoice(self, mv_line_dicts):
        """ Find an open invoice that matches the statement line and which
        could be reconciled with. """
        invoice_line_obj = self.env['account.invoice.line']
        inv_lines = invoice_line_obj
        for mv_line_dict in mv_line_dicts:
            amount = mv_line_dict['credit']
            inv_lines |= invoice_line_obj.search([
                ('partner_id', 'child_of', mv_line_dict.get('partner_id')),
                ('invoice_id.state', 'in', ('open', 'draft')),
                ('product_id', '=', mv_line_dict.get('product_id')),
                ('price_subtotal', '=', amount)])

        return inv_lines.mapped('invoice_id').filtered(
            lambda i: i.amount_total == self.amount)

    def _reconcile(self, matching_records):
        # Now reconcile (code copied from L707)
        counterpart_aml_dicts = []
        payment_aml_rec = self.env['account.move.line']
        for aml in matching_records:
            if aml.account_id.internal_type == 'liquidity':
                payment_aml_rec = (payment_aml_rec | aml)
            else:
                amount = aml.currency_id and \
                    aml.amount_residual_currency or \
                    aml.amount_residual
                counterpart_aml_dicts.append({
                    'name': aml.name if aml.name != '/' else
                    aml.move_id.name,
                    'debit': amount < 0 and -amount or 0,
                    'credit': amount > 0 and amount or 0,
                    'move_line': aml
                })

        try:
            with self._cr.savepoint():
                counterpart = self.process_reconciliation(
                    counterpart_aml_dicts=counterpart_aml_dicts,
                    payment_aml_rec=payment_aml_rec)
            return counterpart
        except UserError:
            self.invalidate_cache()
            self.env['account.move'].invalidate_cache()
            self.env['account.move.line'].invalidate_cache()
            return False
