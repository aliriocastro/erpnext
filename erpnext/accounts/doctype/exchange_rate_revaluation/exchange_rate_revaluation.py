# Copyright (c) 2018, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt


import frappe
from frappe import _, qb
from frappe.model.document import Document
from frappe.model.meta import get_field_precision
from frappe.query_builder import Criterion, Order
from frappe.query_builder.functions import NullIf, Sum
from frappe.utils import flt, get_link_to_form, nowdate

import erpnext
from erpnext.accounts.doctype.journal_entry.journal_entry import get_balance_on
from erpnext.accounts.utils import get_currency_precision
from erpnext.setup.utils import get_exchange_rate


class ExchangeRateRevaluation(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.accounts.doctype.exchange_rate_revaluation_account.exchange_rate_revaluation_account import (
			ExchangeRateRevaluationAccount,
		)

		accounts: DF.Table[ExchangeRateRevaluationAccount]
		amended_from: DF.Link | None
		company: DF.Link
		gain_loss_booked: DF.Currency
		gain_loss_unbooked: DF.Currency
		posting_date: DF.Date
		rounding_loss_allowance: DF.Float
		total_gain_loss: DF.Currency
	# end: auto-generated types

	def validate(self):
		self.validate_rounding_loss_allowance()
		self.set_total_gain_loss()

	def validate_rounding_loss_allowance(self):
		if not (self.rounding_loss_allowance >= 0 and self.rounding_loss_allowance < 1):
			frappe.throw(_("Rounding Loss Allowance should be between 0 and 1"))

	def set_total_gain_loss(self):
		total_gain_loss = 0

		gain_loss_booked = 0
		gain_loss_unbooked = 0

		for d in self.accounts:
			if not d.zero_balance:
				d.gain_loss = flt(
					d.new_balance_in_base_currency, d.precision("new_balance_in_base_currency")
				) - flt(d.balance_in_base_currency, d.precision("balance_in_base_currency"))

			if d.zero_balance:
				gain_loss_booked += flt(d.gain_loss, d.precision("gain_loss"))
			else:
				gain_loss_unbooked += flt(d.gain_loss, d.precision("gain_loss"))

			total_gain_loss += flt(d.gain_loss, d.precision("gain_loss"))

		self.gain_loss_booked = gain_loss_booked
		self.gain_loss_unbooked = gain_loss_unbooked
		self.total_gain_loss = flt(total_gain_loss, self.precision("total_gain_loss"))

	def validate_mandatory(self):
		if not (self.company and self.posting_date):
			frappe.throw(_("Please select Company and Posting Date to getting entries"))

	def before_submit(self):
		self.remove_accounts_without_gain_loss()

	def remove_accounts_without_gain_loss(self):
		self.accounts = [account for account in self.accounts if account.gain_loss]

		if not self.accounts:
			frappe.throw(_("At least one account with exchange gain or loss is required"))

		frappe.msgprint(
			_("Removing rows without exchange gain or loss"),
			alert=True,
			indicator="yellow",
		)

	def on_cancel(self):
		self.ignore_linked_doctypes = ["GL Entry", "Payment Ledger Entry"]

	@frappe.whitelist()
	def check_journal_and_reversal(self):
		journals_posted = False
		reversals_posted = False

		je = qb.DocType("Journal Entry")
		jea = qb.DocType("Journal Entry Account")
		journals = (
			qb.from_(je)
			.join(jea)
			.on(je.name == jea.parent)
			.select(je.name)
			.distinct()
			.where(
				(jea.reference_type == "Exchange Rate Revaluation")
				& (jea.reference_name == self.name)
				& (jea.docstatus == 1)
				& (je.reversal_of.isnull())  # omit journals that have reversals
			)
			.run(pluck="name")
		)
		if journals:
			# Fork Labotech: upstream exigía igualdad exacta entre lo asentado en el
			# gain/loss account y self.total_gain_loss; con redondeo acumulado en
			# cientos de filas (o filas de signos cruzados) nunca cuadra, el botón
			# "Create Journal Entries" reaparece en el ERR ya asentado y permite
			# duplicar el JV. Basta la existencia de un JV submitted sin reversar
			# que referencie este ERR (el query de arriba ya filtra exactamente eso).
			journals_posted = True

		# Fork Labotech: make_jv_entries puede dejar más de un borrador (par
		# estándar + cruzadas + zero-balance); al someter uno, el botón cambia y
		# los otros quedan fáciles de olvidar. El JS los muestra como aviso.
		draft_journals = (
			qb.from_(je)
			.join(jea)
			.on(je.name == jea.parent)
			.select(je.name)
			.distinct()
			.where(
				(jea.reference_type == "Exchange Rate Revaluation")
				& (jea.reference_name == self.name)
				& (jea.docstatus == 0)
			)
			.run(pluck="name")
		)

		# reverse journals
		reverse_journals = (
			qb.from_(je)
			.join(jea)
			.on(je.name == jea.parent)
			.select(je.name)
			.where(
				(jea.reference_type == "Exchange Rate Revaluation")
				& (jea.reference_name == self.name)
				& (jea.docstatus == 1)
				& (je.reversal_of.notnull())
			)
			.run(pluck="name")
		)
		if reverse_journals:
			reversals_posted = True
		else:
			reversals_posted = False

		return {
			"journals_posted": journals_posted,
			"reversals_posted": reversals_posted,
			"draft_journals": draft_journals,
		}

	def fetch_and_calculate_accounts_data(self):
		accounts = self.get_accounts_data()
		if accounts:
			for acc in accounts:
				if acc.get("gain_loss"):
					self.append("accounts", acc)

	@frappe.whitelist()
	def get_accounts_data(self):
		self.validate_mandatory()
		account_details = self.get_account_balance_from_gle(
			company=self.company,
			posting_date=self.posting_date,
			account=None,
			party_type=None,
			party=None,
			rounding_loss_allowance=self.rounding_loss_allowance,
		)
		accounts_with_new_balance = self.calculate_new_account_balance(
			self.company, self.posting_date, account_details
		)

		if not accounts_with_new_balance:
			self.throw_invalid_response_message(account_details)

		return accounts_with_new_balance

	@staticmethod
	def get_account_balance_from_gle(
		company, posting_date, account, party_type, party, rounding_loss_allowance
	):
		account_details = []

		if company and posting_date:
			company_currency = erpnext.get_company_currency(company)

			acc = qb.DocType("Account")
			if account:
				accounts = [account]
			else:
				res = (
					qb.from_(acc)
					.select(acc.name)
					.where(
						(acc.is_group == 0)
						& (acc.report_type == "Balance Sheet")
						& (acc.root_type.isin(["Asset", "Liability", "Equity"]))
						& (acc.account_type != "Stock")
						& (acc.company == company)
						& (acc.account_currency != company_currency)
					)
					.orderby(acc.name)
					.run(as_list=True)
				)
				accounts = [x[0] for x in res]

			if accounts:
				having_clause = (qb.Field("balance") != qb.Field("balance_in_account_currency")) & (
					(qb.Field("balance_in_account_currency") != 0) | (qb.Field("balance") != 0)
				)

				gle = qb.DocType("GL Entry")

				# conditions
				conditions = []
				conditions.append(gle.account.isin(accounts))
				conditions.append(gle.posting_date.lte(posting_date))
				conditions.append(gle.is_cancelled == 0)

				if party_type:
					conditions.append(gle.party_type == party_type)
				if party:
					conditions.append(gle.party == party)

				account_details = (
					qb.from_(gle)
					.select(
						gle.account,
						gle.party_type,
						gle.party,
						gle.account_currency,
						(Sum(gle.debit_in_account_currency) - Sum(gle.credit_in_account_currency)).as_(
							"balance_in_account_currency"
						),
						(Sum(gle.debit) - Sum(gle.credit)).as_("balance"),
						# we don't need to check if gle.balance is zero.
						(Sum(gle.debit_in_account_currency) - Sum(gle.credit_in_account_currency) == 0).as_(
							"zero_balance"
						),
					)
					.where(Criterion.all(conditions))
					.groupby(gle.account, NullIf(gle.party_type, ""), NullIf(gle.party, ""))
					.having(having_clause)
					.orderby(gle.account)
					.run(as_dict=True)
				)

				# round off balance based on currency precision
				# and consider debit-credit difference allowance
				currency_precision = get_currency_precision()
				rounding_loss_allowance = float(rounding_loss_allowance)
				for acc in account_details:
					acc.balance_in_account_currency = flt(acc.balance_in_account_currency, currency_precision)
					if abs(acc.balance_in_account_currency) <= rounding_loss_allowance:
						acc.balance_in_account_currency = 0

					acc.balance = flt(acc.balance, currency_precision)
					if abs(acc.balance) <= rounding_loss_allowance:
						acc.balance = 0

					acc.zero_balance = (
						True if (acc.balance == 0 or acc.balance_in_account_currency == 0) else False
					)

		return account_details

	@staticmethod
	def calculate_new_account_balance(company, posting_date, account_details):
		accounts = []
		company_currency = erpnext.get_company_currency(company)
		precision = get_field_precision(
			frappe.get_meta("Exchange Rate Revaluation Account").get_field("new_balance_in_base_currency"),
			currency=company_currency,
		)

		if account_details:
			# Handle Accounts with balance in both Account/Base Currency
			for d in [x for x in account_details if not x.zero_balance]:
				new_exchange_rate = get_exchange_rate(d.account_currency, company_currency, posting_date)
				# Fork Labotech (hiperinflación): upstream solo protege la división por
				# cero, y con signos cruzados (base vs moneda de cuenta) la tasa promedio
				# sale negativa y rompe el JV (débitos negativos). Tampoco puede quedar
				# en 0: el JV de revaluación falla en validate con "Row X: Exchange Rate
				# is mandatory". Un balance negativo normal (neg/neg) da tasa positiva
				# válida y autoconsistente (acc_bal * tasa == base_bal), así que solo se
				# descarta la tasa promedio cuando no es positiva. Limitación conocida:
				# en filas de signos cruzados el par del JV no puede llevar la cuenta
				# exactamente a acc_bal * new_rate (ninguna tasa positiva lo logra);
				# queda un residual en base que corridas futuras re-detectan.
				current_average_exchange_rate = (
					d.balance / d.balance_in_account_currency if d.balance_in_account_currency else 0
				)
				if current_average_exchange_rate <= 0:
					last_gle_rate = flt(
						calculate_exchange_rate_using_last_gle(company, d.account, d.party_type, d.party)
					)
					current_average_exchange_rate = (
						last_gle_rate if last_gle_rate > 0 else new_exchange_rate
					)
				new_balance_in_base_currency = flt(d.balance_in_account_currency * new_exchange_rate)
				gain_loss = flt(new_balance_in_base_currency, precision) - flt(d.balance, precision)

				accounts.append(
					{
						"account": d.account,
						"party_type": d.party_type,
						"party": d.party,
						"account_currency": d.account_currency,
						"balance_in_base_currency": d.balance,
						"balance_in_account_currency": d.balance_in_account_currency,
						"zero_balance": d.zero_balance,
						"current_exchange_rate": current_average_exchange_rate,
						"new_exchange_rate": new_exchange_rate,
						"new_balance_in_base_currency": new_balance_in_base_currency,
						"new_balance_in_account_currency": d.balance_in_account_currency,
						"gain_loss": gain_loss,
					}
				)

			# Handle Accounts with '0' balance in Account/Base Currency
			for d in [x for x in account_details if x.zero_balance]:
				if d.balance != 0:
					current_exchange_rate = new_exchange_rate = 0

					new_balance_in_account_currency = 0  # this will be '0'
					new_balance_in_base_currency = 0  # this will be '0'
					gain_loss = flt(new_balance_in_base_currency, precision) - flt(d.balance, precision)
				else:
					new_exchange_rate = 0
					new_balance_in_base_currency = 0
					new_balance_in_account_currency = 0

					current_exchange_rate = (
						calculate_exchange_rate_using_last_gle(company, d.account, d.party_type, d.party)
						or 0.0
					)

					gain_loss = new_balance_in_account_currency - (
						current_exchange_rate * d.balance_in_account_currency
					)

				accounts.append(
					{
						"account": d.account,
						"party_type": d.party_type,
						"party": d.party,
						"account_currency": d.account_currency,
						"balance_in_base_currency": d.balance,
						"balance_in_account_currency": d.balance_in_account_currency,
						"zero_balance": d.zero_balance,
						"current_exchange_rate": current_exchange_rate,
						"new_exchange_rate": new_exchange_rate,
						"new_balance_in_base_currency": new_balance_in_base_currency,
						"new_balance_in_account_currency": new_balance_in_account_currency,
						"gain_loss": gain_loss,
					}
				)

		return accounts

	def throw_invalid_response_message(self, account_details):
		if account_details:
			message = _("No outstanding invoices require exchange rate revaluation")
		else:
			message = _("No outstanding invoices found")
		frappe.msgprint(message)

	def get_for_unrealized_gain_loss_account(self):
		unrealized_exchange_gain_loss_account = frappe.get_cached_value(
			"Company", self.company, "unrealized_exchange_gain_loss_account"
		)
		if not unrealized_exchange_gain_loss_account:
			frappe.throw(
				_("Please set Unrealized Exchange Gain/Loss Account in Company {0}").format(self.company)
			)
		return unrealized_exchange_gain_loss_account

	@frappe.whitelist()
	def make_jv_entries(self):
		frappe.has_permission("Journal Entry", "write", throw=True)
		zero_balance_jv = self.make_jv_for_zero_balance()
		if zero_balance_jv:
			frappe.msgprint(
				f"Zero Balance Journal: {get_link_to_form('Journal Entry', zero_balance_jv.name)}"
			)

		revaluation_jv = self.make_jv_for_revaluation()
		if revaluation_jv:
			frappe.msgprint(f"Revaluation Journal: {get_link_to_form('Journal Entry', revaluation_jv.name)}")

		cross_sign_jv = self.make_jv_for_cross_sign()
		if cross_sign_jv:
			frappe.msgprint(f"Cross Sign Journal: {get_link_to_form('Journal Entry', cross_sign_jv.name)}")

		return {
			"revaluation_jv": revaluation_jv.name if revaluation_jv else None,
			"zero_balance_jv": zero_balance_jv.name if zero_balance_jv else None,
			"cross_sign_jv": cross_sign_jv.name if cross_sign_jv else None,
		}

	def make_jv_for_zero_balance(self):
		if self.gain_loss_booked == 0:
			return

		accounts = [x for x in self.accounts if x.zero_balance]

		if not accounts:
			return

		unrealized_exchange_gain_loss_account = self.get_for_unrealized_gain_loss_account()

		journal_entry = frappe.new_doc("Journal Entry")
		journal_entry.voucher_type = "Exchange Gain Or Loss"
		journal_entry.company = self.company
		journal_entry.posting_date = self.posting_date
		journal_entry.multi_currency = 1

		journal_entry_accounts = []
		for d in accounts:
			journal_account = frappe._dict(
				{
					"account": d.get("account"),
					"party_type": d.get("party_type"),
					"party": d.get("party"),
					"account_currency": d.get("account_currency"),
					"balance": flt(
						d.get("balance_in_account_currency"), d.precision("balance_in_account_currency")
					),
					"exchange_rate": 0,
					"cost_center": erpnext.get_default_cost_center(self.company),
					"reference_type": "Exchange Rate Revaluation",
					"reference_name": self.name,
				}
			)

			# Account Currency has balance
			if d.get("balance_in_account_currency") and not d.get("new_balance_in_account_currency"):
				dr_or_cr = (
					"credit_in_account_currency"
					if d.get("balance_in_account_currency") > 0
					else "debit_in_account_currency"
				)
				reverse_dr_or_cr = (
					"debit_in_account_currency"
					if dr_or_cr == "credit_in_account_currency"
					else "credit_in_account_currency"
				)
				journal_account.update(
					{
						dr_or_cr: flt(
							abs(d.get("balance_in_account_currency")),
							d.precision("balance_in_account_currency"),
						),
						reverse_dr_or_cr: 0,
						"debit": 0,
						"credit": 0,
					}
				)

				journal_entry_accounts.append(journal_account)

				journal_entry_accounts.append(
					{
						"account": unrealized_exchange_gain_loss_account,
						"balance": get_balance_on(unrealized_exchange_gain_loss_account),
						"debit": 0,
						"credit": 0,
						"debit_in_account_currency": abs(d.gain_loss) if d.gain_loss < 0 else 0,
						"credit_in_account_currency": abs(d.gain_loss) if d.gain_loss > 0 else 0,
						"cost_center": erpnext.get_default_cost_center(self.company),
						"exchange_rate": 1,
						"reference_type": "Exchange Rate Revaluation",
						"reference_name": self.name,
					}
				)

			elif d.get("balance_in_base_currency") and not d.get("new_balance_in_base_currency"):
				# Base currency has balance
				dr_or_cr = "credit" if d.get("balance_in_base_currency") > 0 else "debit"
				reverse_dr_or_cr = "debit" if dr_or_cr == "credit" else "credit"
				journal_account.update(
					{
						dr_or_cr: flt(
							abs(d.get("balance_in_base_currency")), d.precision("balance_in_base_currency")
						),
						reverse_dr_or_cr: 0,
						"debit_in_account_currency": 0,
						"credit_in_account_currency": 0,
					}
				)

				journal_entry_accounts.append(journal_account)

				journal_entry_accounts.append(
					{
						"account": unrealized_exchange_gain_loss_account,
						"balance": get_balance_on(unrealized_exchange_gain_loss_account),
						"debit": abs(d.gain_loss) if d.gain_loss < 0 else 0,
						"credit": abs(d.gain_loss) if d.gain_loss > 0 else 0,
						"debit_in_account_currency": 0,
						"credit_in_account_currency": 0,
						"cost_center": erpnext.get_default_cost_center(self.company),
						"exchange_rate": 1,
						"reference_type": "Exchange Rate Revaluation",
						"reference_name": self.name,
					}
				)

		journal_entry.set("accounts", journal_entry_accounts)
		journal_entry.set_total_debit_credit()
		journal_entry.save()
		return journal_entry

	def make_jv_for_revaluation(self):
		# Fork Labotech: no gatear con gain_loss_unbooked == 0 — ese total incluye
		# las filas cruzadas (que se asientan aparte) y una cancelación exacta
		# entre cruzadas y normales dejaría filas normales sin asentar.
		accounts = [x for x in self.accounts if not x.zero_balance and not is_cross_sign(x)]
		if not any(flt(x.gain_loss) for x in accounts):
			return

		unrealized_exchange_gain_loss_account = self.get_for_unrealized_gain_loss_account()

		journal_entry = frappe.new_doc("Journal Entry")
		journal_entry.voucher_type = "Exchange Rate Revaluation"
		journal_entry.company = self.company
		journal_entry.posting_date = self.posting_date
		journal_entry.multi_currency = 1

		# Prevent JE from overriding user-entered exchange rates (e.g., rate of 1)
		journal_entry.flags.ignore_exchange_rate = True

		journal_entry_accounts = []
		for d in accounts:
			if not flt(d.get("balance_in_account_currency"), d.precision("balance_in_account_currency")):
				continue

			dr_or_cr = (
				"debit_in_account_currency"
				if d.get("balance_in_account_currency") > 0
				else "credit_in_account_currency"
			)

			reverse_dr_or_cr = (
				"debit_in_account_currency"
				if dr_or_cr == "credit_in_account_currency"
				else "credit_in_account_currency"
			)

			journal_entry_accounts.append(
				{
					"account": d.get("account"),
					"party_type": d.get("party_type"),
					"party": d.get("party"),
					"account_currency": d.get("account_currency"),
					"balance": flt(
						d.get("balance_in_account_currency"), d.precision("balance_in_account_currency")
					),
					dr_or_cr: flt(
						abs(d.get("balance_in_account_currency")), d.precision("balance_in_account_currency")
					),
					"cost_center": erpnext.get_default_cost_center(self.company),
					"exchange_rate": flt(d.get("new_exchange_rate"), d.precision("new_exchange_rate")),
					"reference_type": "Exchange Rate Revaluation",
					"reference_name": self.name,
				}
			)
			journal_entry_accounts.append(
				{
					"account": d.get("account"),
					"party_type": d.get("party_type"),
					"party": d.get("party"),
					"account_currency": d.get("account_currency"),
					"balance": flt(
						d.get("balance_in_account_currency"), d.precision("balance_in_account_currency")
					),
					reverse_dr_or_cr: flt(
						abs(d.get("balance_in_account_currency")), d.precision("balance_in_account_currency")
					),
					"cost_center": erpnext.get_default_cost_center(self.company),
					"exchange_rate": flt(
						d.get("current_exchange_rate"), d.precision("current_exchange_rate")
					),
					"reference_type": "Exchange Rate Revaluation",
					"reference_name": self.name,
				}
			)

		# Fork Labotech: si todas las filas resultaron cruzadas el par queda vacío
		# y la fila balanceadora 0/0 reventaría validate ("Both Debit and Credit
		# values cannot be zero" — este voucher_type no tiene bypass).
		if not journal_entry_accounts:
			return None

		journal_entry.set("accounts", journal_entry_accounts)
		journal_entry.set_amounts_in_company_currency()
		journal_entry.set_total_debit_credit()

		self.gain_loss_unbooked += journal_entry.difference - self.gain_loss_unbooked
		if journal_entry.difference:
			journal_entry.append(
				"accounts",
				{
					"account": unrealized_exchange_gain_loss_account,
					"balance": get_balance_on(unrealized_exchange_gain_loss_account),
					"debit_in_account_currency": abs(self.gain_loss_unbooked)
					if self.gain_loss_unbooked < 0
					else 0,
					"credit_in_account_currency": self.gain_loss_unbooked
					if self.gain_loss_unbooked > 0
					else 0,
					"cost_center": erpnext.get_default_cost_center(self.company),
					"exchange_rate": 1,
					"reference_type": "Exchange Rate Revaluation",
					"reference_name": self.name,
				},
			)

		journal_entry.set_amounts_in_company_currency()
		journal_entry.set_total_debit_credit()
		journal_entry.save()
		return journal_entry

	def make_jv_for_cross_sign(self):
		"""
		Fork Labotech: filas con saldo base y saldo en moneda de cuenta de signos
		opuestos (reconversión, asientos base-only, liquidaciones a tasas muy
		distintas de la de origen). El par estándar no puede llevarlas a
		acc * new_rate, así que el ajuste completo (d.gain_loss = acc*new - base)
		se asienta base-only contra el gain/loss no realizado en un JV separado
		tipo "Exchange Gain Or Loss": con ese voucher_type,
		set_amounts_in_company_currency NO recalcula debit/credit desde la moneda
		de cuenta (en el JV de revaluación la fila base-only quedaría en 0/0 y
		validate la rechazaría). Mismo patrón que la rama base de
		make_jv_for_zero_balance. La cuenta queda en acc * new_rate (±0.01) sin
		tocar el saldo en moneda de cuenta; la próxima corrida del ERR la ve como
		fila normal. El JV nace en borrador: revisar sus filas antes de asentar
		(cuentas de partes relacionadas pueden requerir tratamiento manual).
		"""
		accounts = [
			d
			for d in self.accounts
			if not d.zero_balance
			and flt(d.balance_in_account_currency, d.precision("balance_in_account_currency"))
			and flt(d.gain_loss)
			and is_cross_sign(d)
		]
		if not accounts:
			return None

		unrealized_exchange_gain_loss_account = self.get_for_unrealized_gain_loss_account()

		journal_entry = frappe.new_doc("Journal Entry")
		journal_entry.voucher_type = "Exchange Gain Or Loss"
		journal_entry.company = self.company
		journal_entry.posting_date = self.posting_date
		journal_entry.multi_currency = 1

		journal_entry_accounts = []
		for d in accounts:
			# usar el gain_loss ALMACENADO: es lo que el ERR reporta y suma en
			# total_gain_loss; recomputar acc*new - base puede diferir ±0.01
			adjustment = flt(d.gain_loss)
			dr_or_cr = "debit" if adjustment > 0 else "credit"
			reverse_dr_or_cr = "credit" if dr_or_cr == "debit" else "debit"

			journal_entry_accounts.append(
				{
					"account": d.account,
					"party_type": d.party_type,
					"party": d.party,
					"account_currency": d.account_currency,
					"balance": flt(
						d.balance_in_account_currency, d.precision("balance_in_account_currency")
					),
					dr_or_cr: abs(adjustment),
					reverse_dr_or_cr: 0,
					"debit_in_account_currency": 0,
					"credit_in_account_currency": 0,
					# tasa explícita y sin flags.ignore_exchange_rate: con tasa 0 el
					# validate lanzaría "Exchange Rate is mandatory"; los montos no
					# dependen de ella (voucher_type salta el recálculo)
					"exchange_rate": flt(d.new_exchange_rate),
					"cost_center": erpnext.get_default_cost_center(self.company),
					"reference_type": "Exchange Rate Revaluation",
					"reference_name": self.name,
				}
			)
			journal_entry_accounts.append(
				{
					"account": unrealized_exchange_gain_loss_account,
					"balance": get_balance_on(unrealized_exchange_gain_loss_account),
					reverse_dr_or_cr: abs(adjustment),
					dr_or_cr: 0,
					"debit_in_account_currency": 0,
					"credit_in_account_currency": 0,
					"exchange_rate": 1,
					"cost_center": erpnext.get_default_cost_center(self.company),
					"reference_type": "Exchange Rate Revaluation",
					"reference_name": self.name,
				}
			)

		journal_entry.set("accounts", journal_entry_accounts)
		journal_entry.set_total_debit_credit()
		journal_entry.save()
		return journal_entry

	@frappe.whitelist()
	def make_reverse_journal(self):
		frappe.has_permission("Journal Entry", "write", throw=True)
		je = qb.DocType("Journal Entry")
		jea = qb.DocType("Journal Entry Account")
		journals = (
			qb.from_(je)
			.join(jea)
			.on(je.name == jea.parent)
			.select(je.name)
			.distinct()
			.where(
				(jea.reference_type == "Exchange Rate Revaluation")
				& (jea.reference_name == self.name)
				& (jea.docstatus == 1)
				& (je.reversal_of.isnull())  # omit journals that have reversals
			)
			.run(pluck="name")
		)
		if journals:
			from erpnext.accounts.doctype.journal_entry.journal_entry import make_reverse_journal_entry

			if drafts := frappe.db.get_all(
				"Journal Entry",
				filters={"docstatus": 0, "reversal_of": ["in", journals]},
				pluck="name",
			):
				part = "journals are" if len(drafts) > 1 else "journal is"
				doc_links = ", ".join(["{}".format(get_link_to_form("Journal Entry", x)) for x in drafts])
				frappe.throw(
					msg=_("Reverse {0} already available in draft status: {1}").format(part, doc_links),
				)
			else:
				for x in journals:
					reversal = make_reverse_journal_entry(x)
					reversal.posting_date = nowdate()
					reversal.save()
					frappe.msgprint(
						_("A draft reverse journal for {0} has been created: {1}").format(
							frappe.bold(x), get_link_to_form("Journal Entry", reversal.name)
						)
					)


def is_cross_sign(row):
	"""
	Fork Labotech: saldo base y saldo en moneda de cuenta con signos opuestos
	(ambos no cero). Ninguna tasa positiva puede llevar esa posición a
	acc * new_rate con el par estándar del JV de revaluación; se asienta aparte
	en make_jv_for_cross_sign.
	"""
	return (flt(row.get("balance_in_base_currency")) > 0) != (
		flt(row.get("balance_in_account_currency")) > 0
	)


def calculate_exchange_rate_using_last_gle(company, account, party_type, party):
	"""
	Use last GL entry to calculate exchange rate
	"""
	last_exchange_rate = None
	if company and account:
		gl = qb.DocType("GL Entry")

		# build conditions
		conditions = []
		conditions.append(gl.company == company)
		conditions.append(gl.account == account)
		conditions.append(gl.is_cancelled == 0)
		conditions.append((gl.debit > 0) | (gl.credit > 0))
		conditions.append((gl.debit_in_account_currency > 0) | (gl.credit_in_account_currency > 0))
		if party_type:
			conditions.append(gl.party_type == party_type)
		if party:
			conditions.append(gl.party == party)

		# Fork Labotech: sin ningún GLE que cumpla las condiciones (montos > 0 en
		# base Y en moneda de cuenta), .run()[0] lanzaba IndexError y tumbaba
		# get_accounts_data completo. Alcanzable: cuentas cuyos GLEs solo tienen
		# montos en una de las dos monedas (p.ej. los propios JVs de zero-balance).
		last_voucher = (
			qb.from_(gl)
			.select(gl.voucher_type, gl.voucher_no)
			.where(Criterion.all(conditions))
			.orderby(gl.posting_date, order=Order.desc)
			.limit(1)
			.run()
		)
		if not last_voucher:
			return None
		voucher_type, voucher_no = last_voucher[0]

		last_exchange_rate = (
			qb.from_(gl)
			.select((gl.debit - gl.credit) / (gl.debit_in_account_currency - gl.credit_in_account_currency))
			.where(
				(gl.voucher_type == voucher_type) & (gl.voucher_no == voucher_no) & (gl.account == account)
			)
			.orderby(gl.posting_date, order=Order.desc)
			.limit(1)
			.run()[0][0]
		)

	return last_exchange_rate


@frappe.whitelist()
def get_account_details(
	company, posting_date, account, party_type=None, party=None, rounding_loss_allowance: float | None = None
):
	if not account:
		return
	frappe.has_permission("Account", doc=account, throw=True)

	if not (company and posting_date):
		frappe.throw(_("Company and Posting Date is mandatory"))

	account_currency, account_type = frappe.get_cached_value(
		"Account", account, ["account_currency", "account_type"]
	)

	if account_type in ["Receivable", "Payable"] and not (party_type and party):
		frappe.throw(_("Party Type and Party is mandatory for {0} account").format(account_type))

	account_details = {}
	erpnext.get_company_currency(company)

	account_details = {
		"account_currency": account_currency,
	}
	account_balance = ExchangeRateRevaluation.get_account_balance_from_gle(
		company=company,
		posting_date=posting_date,
		account=account,
		party_type=party_type,
		party=party,
		rounding_loss_allowance=rounding_loss_allowance,
	)

	if account_balance and (account_balance[0].balance or account_balance[0].balance_in_account_currency):
		if account_with_new_balance := ExchangeRateRevaluation.calculate_new_account_balance(
			company, posting_date, account_balance
		):
			row = account_with_new_balance[0]
			account_details.update(
				{
					"balance_in_base_currency": row["balance_in_base_currency"],
					"balance_in_account_currency": row["balance_in_account_currency"],
					"current_exchange_rate": row["current_exchange_rate"],
					"new_exchange_rate": row["new_exchange_rate"],
					"new_balance_in_base_currency": row["new_balance_in_base_currency"],
					"new_balance_in_account_currency": row["new_balance_in_account_currency"],
					"zero_balance": row["zero_balance"],
					"gain_loss": row["gain_loss"],
				}
			)

	return account_details
