"""Shared guest-intake validation; blanks never mean no allergies or occasion."""
import re

def validate_guest_fields(data):
    if any(data.get(k) is not None and not isinstance(data.get(k),str) for k in ('guest_name','guest_email','guest_phone','dietary_requirements','special_occasion')):raise ValueError('Guest details must be text.')
    fields={k:str(data.get(k) or '').strip() for k in ('guest_name','guest_email','guest_phone','dietary_requirements','special_occasion')}
    if not 1<=len(fields['guest_name'])<=120:raise ValueError('Enter the guest name.')
    if len(fields['guest_email'])>200 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',fields['guest_email']):raise ValueError('Enter a valid guest email address.')
    phone=fields['guest_phone']
    if len(phone)>60 or not re.fullmatch(r'[+0-9().\s-]+',phone) or not 7<=len(re.sub(r'\D','',phone))<=15:raise ValueError('Enter a valid guest phone number.')
    if not 1<=len(fields['dietary_requirements'])<=1000:raise ValueError('Ask about allergies and dietary needs. Enter the answer or None explicitly.')
    if not 1<=len(fields['special_occasion'])<=500:raise ValueError('Ask about special occasions. Enter the answer or No special occasion explicitly.')
    fields['guest_email']=fields['guest_email'].lower()
    return fields
