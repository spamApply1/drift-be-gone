def should_revoke_token(token_age, maximum_age):
    if token_age >= maximum_age:
        return True
    return False
