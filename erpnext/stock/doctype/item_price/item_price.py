# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document

from timeit import default_timer as timer

class ItemPriceDuplicateItem(frappe.ValidationError):
	pass


class ItemPrice(Document):

	def validate(self):
		self.validate_item()
		self.validate_dates()
		self.update_price_list_details()
		self.update_item_details()


		disable_duplicates_validation = frappe.db.get_single_value("Stock Settings", "disable_item_price_duplicates_validation")
		if not disable_duplicates_validation:
			self.check_duplicates()

		#self.check_duplicates_in_memory()

	def validate_item(self):
		if not frappe.db.exists("Item", self.item_code):
			frappe.throw(_("Item {0} not found.").format(self.item_code))

	def validate_dates(self):
		if self.valid_from and self.valid_upto:
			if self.valid_from > self.valid_upto:
				frappe.throw(_("Valid From Date must be lesser than Valid Upto Date."))

	def update_price_list_details(self):
		if self.price_list:
			price_list_details = frappe.db.get_value("Price List",
				{"name": self.price_list, "enabled": 1},
				["buying", "selling", "currency"])

			if not price_list_details:
				link = frappe.utils.get_link_to_form('Price List', self.price_list)
				frappe.throw("The price list {0} does not exist or is disabled".format(link))

			self.buying, self.selling, self.currency = price_list_details

	def update_item_details(self):
		if self.item_code:
			self.item_name, self.item_description = frappe.db.get_value("Item", self.item_code,["item_name", "description"])

	def check_duplicates(self):
		conditions = """where item_code = %(item_code)s and price_list = %(price_list)s and name != %(name)s"""

		for field in [
			"uom",
			"valid_from",
			"valid_upto",
			"packing_unit",
			"customer",
			"supplier",
			"batch_no"]:
			if self.get(field):
				conditions += " and {0} = %({0})s ".format(field)
			else:
				conditions += "and (isnull({0}) or {0} = '')".format(field)

		price_list_rate = frappe.db.sql("""
				select price_list_rate
				from `tabItem Price`
				{conditions}
			""".format(conditions=conditions),
			self.as_dict(),)

		if price_list_rate :
			frappe.throw(_("Item Price appears multiple times based on Price List, Supplier/Customer, Currency, Item, UOM, Qty and Dates."), ItemPriceDuplicateItem)

	def check_duplicates_in_memory(self):
		def _item_prices_data_generator(price_list):
			item_prices = frappe.db.sql("""SELECT item_code, price_list, name, uom, valid_from, valid_upto, packing_unit, customer, supplier
						FROM `tabItem Price`
						WHERE price_list=%(price_list)s""", {"price_list": price_list}, as_dict=1)

			return item_prices

		def log_execution_time(st, et):
			frappe.log_error(f"Execution time: {((et-st)*1000)}ms ", "Item Price Execution Time")

		start_time = timer()

		cache_key =  f"item_prices.{frappe.scrub(self.price_list)}"
		#cache_key =  f"item_prices"
		data = frappe.cache().get_value(cache_key)

		time_cache_get_value = timer()

		if data is None:
			frappe.log_error(f"NOT Found {cache_key} in cache.", "Item Price: NOT FOUND")
			data = _item_prices_data_generator(self.price_list)
			#frappe.cache().set_value(cache_key, data, expires_in_sec=15)
			frappe.cache().set_value(cache_key, data)

		time_cache_gen_data = timer()

		data = list(filter(lambda x: x.get("item_code") == self.item_code and x.get("name") != self.name, data))

		time_first_filtration = timer()

		for field in ['uom', 'valid_from',
					'valid_upto', 'packing_unit', 'customer', 'supplier']:
			if self.get(field):
				data = list(filter(lambda x: x.get(field) == self.get(field), data))

		time_sec_filtration = timer()

		if len(data) > 0:
			log_execution_time(start_time, timer())
			frappe.log_error(frappe.as_json(data), "Item Price appears multiple times")
			frappe.throw(_("Item Price appears multiple times based on Price List, Supplier/Customer, Currency, Item, UOM, Qty and Dates."), ItemPriceDuplicateItem)

		# frappe.log_error(frappe.as_json({
		# 	"total": (timer() - start_time)*1000,
		# 	"gen_data": (time_cache_gen_data - time_cache_get_value)*1000,
		# 	"cache_get_value": (time_cache_get_value - start_time)*1000,
		# 	"first_filtration": (time_first_filtration - time_cache_gen_data)*1000,
		# 	"sec_filtration": (timer() - time_sec_filtration)*1000
		# }), "Item Price Profiling")


	def before_save(self):
		if self.selling:
			self.reference = self.customer
		if self.buying:
			self.reference = self.supplier

		if self.selling and not self.buying:
			# if only selling then remove supplier
			self.supplier = None
		if self.buying and not self.selling:
			# if only buying then remove customer
			self.customer = None
