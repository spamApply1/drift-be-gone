def calculate_invoice_total(items, tax_rate):
    subtotal = 0
    for item in items:
        subtotal += item["unit_price"] * item["quantity"]
    return subtotal + subtotal * tax_rate
