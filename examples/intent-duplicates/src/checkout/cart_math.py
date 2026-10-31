def compute_cart_charge(products, vat_rate):
    running_total = 0
    for product in products:
        running_total += product["price"] * product["count"]
    return running_total + running_total * vat_rate
