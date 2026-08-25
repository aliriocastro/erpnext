# Copyright (c) 2018, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt


import frappe
from frappe.tests.utils import FrappeTestCase, change_settings
from frappe.utils import add_days, flt, today

from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from erpnext.accounts.doctype.sales_invoice.test_sales_invoice import create_sales_invoice
from erpnext.accounts.test.accounts_mixin import AccountsTestMixin


class TestExchangeRateRevaluation(AccountsTestMixin, FrappeTestCase):
	def setUp(self):
		self.create_company()
		self.create_usd_receivable_account()
		self.create_item()
		self.create_customer()
		self.clear_old_entries()
		self.set_system_and_company_settings()

	def tearDown(self):
		frappe.db.rollback()

	def set_system_and_company_settings(self):
		# set number and currency precision
		system_settings = frappe.get_doc("System Settings")
		system_settings.float_precision = 2
		system_settings.currency_precision = 2
		system_settings.save()

		# Using Exchange Gain/Loss account for unrealized as well.
		company_doc = frappe.get_doc("Company", self.company)
		company_doc.unrealized_exchange_gain_loss_account = company_doc.exchange_gain_loss_account
		company_doc.save()

	@change_settings(
		"Accounts Settings",
		{"allow_multi_currency_invoices_against_single_party_account": 1, "allow_stale": 0},
	)
	def test_01_revaluation_of_forex_balance(self):
		"""
		Test Forex account balance and Journal creation post Revaluation
		"""
		si = create_sales_invoice(
			item=self.item,
			company=self.company,
			customer=self.customer,
			debit_to=self.debtors_usd,
			posting_date=today(),
			parent_cost_center=self.cost_center,
			cost_center=self.cost_center,
			rate=100,
			price_list_rate=100,
			do_not_submit=1,
		)
		si.currency = "USD"
		si.conversion_rate = 80
		si.save().submit()

		err = frappe.new_doc("Exchange Rate Revaluation")
		err.company = self.company
		err.posting_date = today()
		accounts = err.get_accounts_data()
		err.extend("accounts", accounts)
		row = err.accounts[0]
		row.new_exchange_rate = 85
		row.new_balance_in_base_currency = flt(row.new_exchange_rate * flt(row.balance_in_account_currency))
		row.gain_loss = row.new_balance_in_base_currency - flt(row.balance_in_base_currency)
		err.set_total_gain_loss()
		err = err.save().submit()

		# Create JV for ERR
		err_journals = err.make_jv_entries()
		je = frappe.get_doc("Journal Entry", err_journals.get("revaluation_jv"))
		je = je.submit()

		je.reload()
		self.assertEqual(je.voucher_type, "Exchange Rate Revaluation")
		self.assertEqual(je.total_debit, 8500.0)
		self.assertEqual(je.total_credit, 8500.0)

		acc_balance = frappe.db.get_all(
			"GL Entry",
			filters={"account": self.debtors_usd, "is_cancelled": 0},
			fields=["sum(debit)-sum(credit) as balance"],
		)[0]
		self.assertEqual(acc_balance.balance, 8500.0)

	@change_settings(
		"Accounts Settings",
		{"allow_multi_currency_invoices_against_single_party_account": 1, "allow_stale": 0},
	)
	def test_02_accounts_only_with_base_currency_balance(self):
		"""
		Test Revaluation on Forex account with balance only in base currency
		"""
		si = create_sales_invoice(
			item=self.item,
			company=self.company,
			customer=self.customer,
			debit_to=self.debtors_usd,
			posting_date=today(),
			parent_cost_center=self.cost_center,
			cost_center=self.cost_center,
			rate=100,
			price_list_rate=100,
			do_not_submit=1,
		)
		si.currency = "USD"
		si.conversion_rate = 80
		si.save().submit()

		pe = get_payment_entry(si.doctype, si.name)
		pe.source_exchange_rate = 85
		pe.received_amount = 8500
		pe.save().submit()

		# Cancel the auto created gain/loss JE to simulate balance only in base currency
		je = frappe.db.get_all("Journal Entry Account", filters={"reference_name": si.name}, pluck="parent")[
			0
		]
		frappe.get_doc("Journal Entry", je).cancel()

		err = frappe.new_doc("Exchange Rate Revaluation")
		err.company = self.company
		err.posting_date = today()
		err.fetch_and_calculate_accounts_data()
		err = err.save().submit()

		# Create JV for ERR
		ret = err.check_journal_and_reversal()
		self.assertFalse(ret.get("journals_posted"))
		err_journals = err.make_jv_entries()
		je = frappe.get_doc("Journal Entry", err_journals.get("zero_balance_jv"))
		je = je.submit()

		je.reload()
		self.assertEqual(je.voucher_type, "Exchange Gain Or Loss")
		self.assertEqual(len(je.accounts), 2)
		# Only base currency fields will be posted to
		for acc in je.accounts:
			self.assertEqual(acc.debit_in_account_currency, 0)
			self.assertEqual(acc.credit_in_account_currency, 0)

		self.assertEqual(je.total_debit, 500.0)
		self.assertEqual(je.total_credit, 500.0)

		acc_balance = frappe.db.get_all(
			"GL Entry",
			filters={"account": self.debtors_usd, "is_cancelled": 0},
			fields=[
				"sum(debit)-sum(credit) as balance",
				"sum(debit_in_account_currency)-sum(credit_in_account_currency) as balance_in_account_currency",
			],
		)[0]
		# account shouldn't have balance in base and account currency
		self.assertEqual(acc_balance.balance, 0.0)
		self.assertEqual(acc_balance.balance_in_account_currency, 0.0)

	@change_settings(
		"Accounts Settings",
		{"allow_multi_currency_invoices_against_single_party_account": 1, "allow_stale": 0},
	)
	def test_03_accounts_only_with_account_currency_balance(self):
		"""
		Test Revaluation on Forex account with balance only in account currency
		"""
		precision = frappe.db.get_single_value("System Settings", "currency_precision")

		# posting on previous date to make sure that ERR picks up the Payment entry's exchange
		# rate while calculating gain/loss for account currency balance
		si = create_sales_invoice(
			item=self.item,
			company=self.company,
			customer=self.customer,
			debit_to=self.debtors_usd,
			posting_date=add_days(today(), -1),
			parent_cost_center=self.cost_center,
			cost_center=self.cost_center,
			rate=100,
			price_list_rate=100,
			do_not_submit=1,
		)
		si.currency = "USD"
		si.conversion_rate = 80
		si.save().submit()

		pe = get_payment_entry(si.doctype, si.name)
		pe.paid_amount = 95
		pe.source_exchange_rate = 84.2105
		pe.received_amount = 8000
		pe.references = []
		pe.save().submit()

		acc_balance = frappe.db.get_all(
			"GL Entry",
			filters={"account": self.debtors_usd, "is_cancelled": 0},
			fields=[
				"sum(debit)-sum(credit) as balance",
				"sum(debit_in_account_currency)-sum(credit_in_account_currency) as balance_in_account_currency",
			],
		)[0]
		# account should have balance only in account currency
		self.assertEqual(flt(acc_balance.balance, precision), 0.0)
		self.assertEqual(flt(acc_balance.balance_in_account_currency, precision), 5.0)  # in USD

		err = frappe.new_doc("Exchange Rate Revaluation")
		err.company = self.company
		err.posting_date = today()
		err.fetch_and_calculate_accounts_data()
		err.set_total_gain_loss()
		err = err.save().submit()

		# Create JV for ERR
		ret = err.check_journal_and_reversal()
		self.assertFalse(ret.get("journals_posted"))
		err_journals = err.make_jv_entries()
		je = frappe.get_doc("Journal Entry", err_journals.get("zero_balance_jv"))
		je = je.submit()

		je.reload()
		self.assertEqual(je.voucher_type, "Exchange Gain Or Loss")
		self.assertEqual(len(je.accounts), 2)
		# Only account currency fields will be posted to
		for acc in je.accounts:
			self.assertEqual(flt(acc.debit, precision), 0.0)
			self.assertEqual(flt(acc.credit, precision), 0.0)

		row = next(x for x in je.accounts if x.account == self.debtors_usd)
		self.assertEqual(flt(row.credit_in_account_currency, precision), 5.0)  # in USD
		row = next(x for x in je.accounts if x.account != self.debtors_usd)
		self.assertEqual(flt(row.debit_in_account_currency, precision), 421.05)  # in INR

		# total_debit and total_credit will be 0.0, as JV is posting only to account currency fields
		self.assertEqual(flt(je.total_debit, precision), 0.0)
		self.assertEqual(flt(je.total_credit, precision), 0.0)

		acc_balance = frappe.db.get_all(
			"GL Entry",
			filters={"account": self.debtors_usd, "is_cancelled": 0},
			fields=[
				"sum(debit)-sum(credit) as balance",
				"sum(debit_in_account_currency)-sum(credit_in_account_currency) as balance_in_account_currency",
			],
		)[0]
		# account shouldn't have balance in base and account currency post revaluation
		self.assertEqual(flt(acc_balance.balance, precision), 0.0)
		self.assertEqual(flt(acc_balance.balance_in_account_currency, precision), 0.0)

	@change_settings(
		"Accounts Settings",
		{"allow_multi_currency_invoices_against_single_party_account": 1, "allow_stale": 0},
	)
	def test_04_get_account_details_function(self):
		si = create_sales_invoice(
			item=self.item,
			company=self.company,
			customer=self.customer,
			debit_to=self.debtors_usd,
			posting_date=today(),
			parent_cost_center=self.cost_center,
			cost_center=self.cost_center,
			rate=100,
			price_list_rate=100,
			do_not_submit=1,
		)
		si.currency = "USD"
		si.conversion_rate = 80
		si.save().submit()

		from erpnext.accounts.doctype.exchange_rate_revaluation.exchange_rate_revaluation import (
			get_account_details,
		)

		account_details = get_account_details(
			self.company, si.posting_date, self.debtors_usd, "Customer", self.customer, 0.05
		)
		# not checking for new exchange rate and balances as it is dependent on live exchange rates
		expected_data = {
			"account_currency": "USD",
			"balance_in_base_currency": 8000.0,
			"balance_in_account_currency": 100.0,
			"current_exchange_rate": 80.0,
			"zero_balance": False,
			"new_balance_in_account_currency": 100.0,
		}

		for key, _val in expected_data.items():
			self.assertEqual(expected_data.get(key), account_details.get(key))

	@change_settings(
		"Accounts Settings",
		{"allow_multi_currency_invoices_against_single_party_account": 1, "allow_stale": 0},
	)
	def test_05_revaluation_journal_reversal(self):
		"""
		Test reversing of revaluation journals
		"""
		si = create_sales_invoice(
			item=self.item,
			company=self.company,
			customer=self.customer,
			debit_to=self.debtors_usd,
			posting_date=today(),
			parent_cost_center=self.cost_center,
			cost_center=self.cost_center,
			rate=100,
			price_list_rate=100,
			do_not_submit=1,
		)
		si.currency = "USD"
		si.conversion_rate = 80
		si.save().submit()

		err = frappe.new_doc("Exchange Rate Revaluation")
		err.company = self.company
		err.posting_date = today()
		err.fetch_and_calculate_accounts_data()
		self.assertEqual(len(err.accounts), 1)
		err.save().submit()

		gain_loss_account = err.get_for_unrealized_gain_loss_account()
		usd_account = err.accounts[0].account
		old_balance = err.accounts[0].balance_in_base_currency
		new_balance = err.accounts[0].new_balance_in_base_currency
		total_gain_loss = err.total_gain_loss

		# Create JV for ERR
		ret = err.check_journal_and_reversal()
		self.assertFalse(ret.get("journals_posted"))
		err_journals = err.make_jv_entries()
		je = frappe.get_doc("Journal Entry", err_journals.get("revaluation_jv"))
		je = je.submit()

		je.reload()
		self.assertEqual(je.voucher_type, "Exchange Rate Revaluation")
		self.assertEqual(len(je.accounts), 3)
		expected = [
			(usd_account, new_balance, 0.0, 100.0, 0.0),
			(usd_account, 0.0, old_balance, 0.0, 100.0),
			(gain_loss_account, 0.0, total_gain_loss, 0.0, total_gain_loss),
		]
		actual = []
		for acc in je.accounts:
			actual.append(
				(
					acc.account,
					acc.debit,
					acc.credit,
					acc.debit_in_account_currency,
					acc.credit_in_account_currency,
				)
			)
		self.assertEqual(expected, actual)

		# Assert reversals are not posted
		ret = err.check_journal_and_reversal()
		self.assertTrue(ret.get("journals_posted"))
		self.assertFalse(ret.get("reversals_posted"))

		err.make_reverse_journal()
		# submit
		draft = frappe.db.get_all(
			"Journal Entry",
			filters={"docstatus": 0, "reversal_of": je.name, "voucher_type": "Exchange Rate Revaluation"},
			pluck="name",
		)
		self.assertIsNotNone(draft)
		frappe.get_doc("Journal Entry", draft[0]).submit()
		ret = err.check_journal_and_reversal()
		self.assertTrue(ret.get("journals_posted"))
		self.assertTrue(ret.get("reversals_posted"))

		reverse_jv = frappe.db.get_all(
			"Journal Entry", filters={"reversal_of": err_journals.get("revaluation_jv")}, pluck="name"
		)
		self.assertIsNotNone(reverse_jv)

	@change_settings(
		"Accounts Settings",
		{"allow_multi_currency_invoices_against_single_party_account": 1, "allow_stale": 0},
	)
	def test_06_revaluation_of_negative_forex_balance(self):
		"""
		Regresión fork Labotech: un balance negativo (neg base / neg moneda de
		cuenta) debe conservar tasa promedio positiva; si queda en 0, el JV de
		revaluación falla con "Row X: Exchange Rate is mandatory".
		"""
		self._ensure_todays_usd_rate()

		# Anticipo de cliente: deja Debtors USD en -100 USD / -8000 base
		je = frappe.new_doc("Journal Entry")
		je.company = self.company
		je.posting_date = today()
		je.multi_currency = 1
		je.append(
			"accounts",
			{
				"account": self.debtors_usd,
				"party_type": "Customer",
				"party": self.customer,
				"account_currency": "USD",
				"exchange_rate": 80,
				"credit_in_account_currency": 100,
				"cost_center": self.cost_center,
			},
		)
		je.append(
			"accounts",
			{
				"account": self.cash,
				"debit_in_account_currency": 8000,
				"cost_center": self.cost_center,
			},
		)
		je.save().submit()

		err = frappe.new_doc("Exchange Rate Revaluation")
		err.company = self.company
		err.posting_date = today()
		err.fetch_and_calculate_accounts_data()
		self.assertEqual(len(err.accounts), 1)

		row = err.accounts[0]
		# la tasa promedio de un balance negativo (neg/neg) es positiva
		self.assertEqual(flt(row.current_exchange_rate), 80.0)

		row.new_exchange_rate = 85
		row.new_balance_in_base_currency = flt(
			row.new_exchange_rate * flt(row.balance_in_account_currency)
		)
		row.gain_loss = row.new_balance_in_base_currency - flt(row.balance_in_base_currency)
		err.set_total_gain_loss()
		err = err.save().submit()

		# antes del fix, current_exchange_rate quedaba en 0 (el assert de la tasa,
		# arriba, falla); en producción ese 0 llegaba al JV y lanzaba
		# "Row X: Exchange Rate is mandatory" (journal_entry.py:919)
		err_journals = err.make_jv_entries()
		revaluation_je = frappe.get_doc("Journal Entry", err_journals.get("revaluation_jv"))
		revaluation_je.submit()
		revaluation_je.reload()
		self.assertEqual(revaluation_je.total_debit, 8500.0)

		# la cuenta queda revaluada exactamente a -100 USD * 85 en base
		acc_balance = frappe.db.get_all(
			"GL Entry",
			filters={"account": self.debtors_usd, "is_cancelled": 0},
			fields=[
				"sum(debit)-sum(credit) as balance",
				"sum(debit_in_account_currency)-sum(credit_in_account_currency) as balance_acc",
			],
		)[0]
		self.assertEqual(flt(acc_balance.balance), -8500.0)
		self.assertEqual(flt(acc_balance.balance_acc), -100.0)

	def _make_forex_je(self, posting_date, party, debit_usd=0, credit_usd=0, rate=80):
		je = frappe.new_doc("Journal Entry")
		je.company = self.company
		je.posting_date = posting_date
		je.multi_currency = 1
		base = flt((debit_usd or credit_usd) * rate)
		je.append(
			"accounts",
			{
				"account": self.debtors_usd,
				"party_type": "Customer",
				"party": party,
				"account_currency": "USD",
				"exchange_rate": rate,
				"debit_in_account_currency": debit_usd,
				"credit_in_account_currency": credit_usd,
				"cost_center": self.cost_center,
			},
		)
		je.append(
			"accounts",
			{
				"account": self.cash,
				"debit_in_account_currency": base if credit_usd else 0,
				"credit_in_account_currency": base if debit_usd else 0,
				"cost_center": self.cost_center,
			},
		)
		je.save().submit()
		return je

	def _ensure_todays_usd_rate(self, rate=83):
		# tasa fija del día: hace determinístico el fetch (el gain_loss vivo nunca
		# se anula por coincidir la tasa de mercado con la del fixture) y evita la
		# llamada al API de tasas con allow_stale=0
		company_currency = frappe.get_cached_value("Company", self.company, "default_currency")
		if not frappe.db.exists(
			"Currency Exchange",
			{"date": today(), "from_currency": "USD", "to_currency": company_currency},
		):
			frappe.get_doc(
				{
					"doctype": "Currency Exchange",
					"date": today(),
					"from_currency": "USD",
					"to_currency": company_currency,
					"exchange_rate": rate,
					"for_buying": 1,
					"for_selling": 1,
				}
			).insert()

	@change_settings(
		"Accounts Settings",
		{"allow_multi_currency_invoices_against_single_party_account": 1, "allow_stale": 0},
	)
	def test_07_revaluation_with_cross_sign_balance(self):
		"""
		Fork Labotech: con signos cruzados (+5 USD / -550 base) el par estándar no
		aplica; la fila sale en un JV base-only aparte que deja la cuenta en
		acc * new_rate y la posición converge en una corrida.
		"""
		self._ensure_todays_usd_rate()

		# ayer: +100 USD @80 (+8000 base); hoy: -95 USD @90 (-8550 base)
		self._make_forex_je(add_days(today(), -1), self.customer, debit_usd=100, rate=80)
		self._make_forex_je(today(), self.customer, credit_usd=95, rate=90)
		# posición cruzada: +5 USD / -550 base → tasa promedio -110

		err = frappe.new_doc("Exchange Rate Revaluation")
		err.company = self.company
		err.posting_date = today()
		err.fetch_and_calculate_accounts_data()
		self.assertEqual(len(err.accounts), 1)

		row = err.accounts[0]
		self.assertEqual(flt(row.balance_in_account_currency), 5.0)
		self.assertEqual(flt(row.balance_in_base_currency), -550.0)
		# fallback: la tasa del último GLE (el JE de hoy @90); nunca 0 ni negativa
		self.assertEqual(flt(row.current_exchange_rate), 90.0)

		row.new_exchange_rate = 85
		row.new_balance_in_base_currency = flt(
			row.new_exchange_rate * flt(row.balance_in_account_currency)
		)
		row.gain_loss = row.new_balance_in_base_currency - flt(row.balance_in_base_currency)
		err.set_total_gain_loss()
		err = err.save().submit()

		# la fila cruzada NO va al par estándar: sale en el JV base-only aparte
		err_journals = err.make_jv_entries()
		self.assertIsNone(err_journals.get("revaluation_jv"))
		self.assertIsNotNone(err_journals.get("cross_sign_jv"))

		cross_jv = frappe.get_doc("Journal Entry", err_journals.get("cross_sign_jv"))
		cross_jv.submit()
		cross_jv.reload()
		self.assertEqual(cross_jv.voucher_type, "Exchange Gain Or Loss")
		# ajuste completo = gain_loss = 5*85 - (-550) = 975
		self.assertEqual(cross_jv.total_debit, 975.0)

		# la cuenta queda exactamente en acc * new_rate sin tocar el saldo USD
		acc_balance = frappe.db.get_all(
			"GL Entry",
			filters={"account": self.debtors_usd, "is_cancelled": 0},
			fields=[
				"sum(debit)-sum(credit) as balance",
				"sum(debit_in_account_currency)-sum(credit_in_account_currency) as balance_acc",
			],
		)[0]
		self.assertEqual(flt(acc_balance.balance), 425.0)
		self.assertEqual(flt(acc_balance.balance_acc), 5.0)

		# tras someter el JV, el ERR reconoce el asiento (guard anti-duplicado)
		ret = err.check_journal_and_reversal()
		self.assertTrue(ret.get("journals_posted"))

		# convergencia: la posición ya no está cruzada (tasa promedio 425/5 = 85);
		# con la tasa del día en 83 el gain_loss vivo es -10, la fila sí aparece
		err2 = frappe.new_doc("Exchange Rate Revaluation")
		err2.company = self.company
		err2.posting_date = today()
		err2.fetch_and_calculate_accounts_data()
		self.assertEqual(len(err2.accounts), 1)
		self.assertEqual(flt(err2.accounts[0].current_exchange_rate), 85.0)

	@change_settings(
		"Accounts Settings",
		{"allow_multi_currency_invoices_against_single_party_account": 1, "allow_stale": 0},
	)
	def test_08_mixed_normal_and_cross_sign_rows(self):
		"""
		Fila normal y fila cruzada conviven: el par estándar asienta la normal, el
		JV base-only la cruzada, y entre ambos asientan exactamente el
		total_gain_loss del ERR.
		"""
		self._ensure_todays_usd_rate()

		cross_customer = "_Test ERR Cross Customer"
		if not frappe.db.exists("Customer", cross_customer):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": cross_customer,
					"customer_type": "Individual",
					"customer_group": "Individual",
					"territory": "All Territories",
				}
			).insert()

		# normal: anticipo del customer 1 → -100 USD / -8000 base
		self._make_forex_je(today(), self.customer, credit_usd=100, rate=80)
		# cruzada: customer 2 → +5 USD / -550 base
		self._make_forex_je(add_days(today(), -1), cross_customer, debit_usd=100, rate=80)
		self._make_forex_je(today(), cross_customer, credit_usd=95, rate=90)

		err = frappe.new_doc("Exchange Rate Revaluation")
		err.company = self.company
		err.posting_date = today()
		err.fetch_and_calculate_accounts_data()
		self.assertEqual(len(err.accounts), 2)

		for row in err.accounts:
			row.new_exchange_rate = 85
			row.new_balance_in_base_currency = flt(
				row.new_exchange_rate * flt(row.balance_in_account_currency)
			)
			row.gain_loss = row.new_balance_in_base_currency - flt(row.balance_in_base_currency)
		err.set_total_gain_loss()
		err = err.save().submit()
		# normal: -8500 - (-8000) = -500 ; cruzada: 425 - (-550) = +975
		self.assertEqual(flt(err.total_gain_loss), 475.0)

		err_journals = err.make_jv_entries()
		self.assertIsNotNone(err_journals.get("revaluation_jv"))
		self.assertIsNotNone(err_journals.get("cross_sign_jv"))
		frappe.get_doc("Journal Entry", err_journals.get("revaluation_jv")).submit()
		frappe.get_doc("Journal Entry", err_journals.get("cross_sign_jv")).submit()

		# lo asentado al gain/loss account entre ambos JVs == total_gain_loss
		gain_loss_account = err.get_for_unrealized_gain_loss_account()
		posted = frappe.db.get_all(
			"GL Entry",
			filters={
				"account": gain_loss_account,
				"is_cancelled": 0,
				"voucher_no": [
					"in",
					[err_journals.get("revaluation_jv"), err_journals.get("cross_sign_jv")],
				],
			},
			fields=["sum(credit)-sum(debit) as net"],
		)[0]
		self.assertEqual(flt(posted.net), 475.0)
