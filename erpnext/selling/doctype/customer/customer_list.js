frappe.listview_settings['Customer'] = {
	add_fields: ["customer_name", "territory", "customer_group", "customer_type", "image"],

	onload(listview) {
		listview.page.add_button("Reconstruir Lista de Precios",
			async () => {

				async function execute_build_all_customers_combined_item_prices() {
					frappe.show_progress('Reconstruyendo Lista de Precios de Todos los Clientes...', 70, 100, 'Espere, este proceso puede demorar un minuto o dos.');
					await frappe.xcall("labotech.labotech.api.client.build_all_customers_combined_item_prices", {})
					frappe.hide_progress();

				}

				await execute_build_all_customers_combined_item_prices();

			}, { icon: 'setting-gear' })
	}
};

