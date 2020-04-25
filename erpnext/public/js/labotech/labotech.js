frappe.ui.form.on('Sales Order Item', {
	async item_code(frm,cdt,cdn) {
	    
	    let uoms = await frappe.db.get_list('UOM Conversion Detail',{ parent: 'Item', fields: 'uom', filters: {'parent': locals[cdt][cdn].item_code}});
	    
	    frm.get_field('items').grid.grid_rows_by_docname[cdn].get_field('uom').get_query = {
	        uom_name: ["in", uoms.map(e => e.uom)]
	    };
		
	}
});