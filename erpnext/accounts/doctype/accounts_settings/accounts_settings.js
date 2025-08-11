// Copyright (c) 2016, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Accounts Settings", {
	refresh: function (frm) {},

	drop_ar_procedures: function (frm) {
		frm.call({
			doc: frm.doc,
			method: "drop_ar_sql_procedures",
			callback: function (r) {
				frappe.show_alert(__("Procedures dropped"), 5);
			},
		});
	},
});
