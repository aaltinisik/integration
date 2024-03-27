# Copyright 2023 Samet Altuntaş (https://github.com/samettal)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl)

from lxml import etree
from bs4 import BeautifulSoup
from hashlib import sha1
from odoo.exceptions import ValidationError
from odoo.addons.payment_garanti.const import PROVISION_URL
from odoo import _
import requests
import time
import re
import logging

_logger = logging.getLogger(__name__)


class GarantiConnector:
    def __init__(self, acquirer, tx, amount, card_args=None, client_ip=None):
        self.url = acquirer._garanti_get_api_url()
        self.provider = acquirer
        self.tx = tx
        self.amount = self._get_amount(amount)
        self.currency_id = self._get_currency_id()
        self.card_args = card_args
        self.client_ip = client_ip
        self.timeout = 10
        self._session = requests.Session()
        self._debug = self.provider.debug_logging

    def __repr__(self):
        return "<GarantiConnector: %s>" % self.tx.reference

    @property
    def reference(self):
        """
        We can't use same reference in payment.transaction but we can send
        same reference to Garanti Sanal Pos API.
        :return:
        """
        return self.tx.reference.split("-")[0]

    def _get_partner_email(self):
        """
        Only first email will be used.
        :return:
        """
        return self.tx.partner_email.split(",")[0]

    def _get_amount(self, amount):
        """Get amount in kuruş.
        Note: convert turkish partner's amount to turkish lira always.
        :param amount: float
        :return: Amount in kuruş
        """
        # amount = self.tx.sale_order_ids.garanti_payment_amount
        return int(round(amount * 100, 0))

    def _get_currency_id(self):
        """Get currency id.
        :return: Currency id
        """
        return self.tx.sale_order_ids.garanti_payment_currency_id.id

    def _process_http_request(self, response):
        """Log HTTP request if debugging enabled and return response.
        :param response: Response
        :return: Boolean
        """
        if not self._debug:
            return True

        def _anonymize_sensitive_data(data):
            # Find and Replace credit cart number if exists, keep the last 4 digits
            card_number = re.search(r"\d{16}", data)
            if card_number:
                data = data.replace(
                    card_number.group(), "****" + card_number.group()[-4:]
                )
            return data

        def _serialize_request(request):
            return _anonymize_sensitive_data(
                "Request: %s %s\n%s\n\n"
                % (
                    request.method,
                    request.url,
                    request.body,
                )
            )

        def _serialize_response(resp):
            return _anonymize_sensitive_data(
                "Response: %s\n%s\n\n" % (resp.status_code, resp.text)
            )

        self.provider.log_xml(_serialize_request(response.request), "garanti_request")
        self.provider.log_xml(_serialize_response(response), "garanti_response")

        return True

    def _garanti_requests(self, method, url, *args, **kwargs):
        """
        Send the request and return the response
        """
        with self._session as http_client:
            response = http_client.request(
                method=method,
                url=url,
                timeout=self.timeout,
                *args,
                **kwargs,
            )
            self._process_http_request(response)
            response.raise_for_status()
            return response

    def _garanti_parse_response_html(self, response):
        """Parse response HTML from Garanti Sanal Pos API.

        :param response: Response
        :return: Response HTML
        """
        soup = BeautifulSoup(response.text, "html.parser")
        error_msg = soup.find("input", {"name": "mderrormessage"})
        if error_msg:
            if error_msg["value"] == "Not Authenticated":
                raise ValidationError(
                    _("Payment failed." " POS: Card is not authenticated.")
                )
            raise ValidationError(_("Payment Error: %s") % error_msg["value"])

        form = soup.find("form", {"id": "webform0"})

        if form:
            return "form", str(form)

        # This means that the response is redirection page
        else:
            return "redirect", str(soup)

    def _garanti_make_payment_request(self):
        """Send payment request to Garanti Sanal Pos API.

        :return: Response
        """
        vals = self._garanti_create_payment_vals()
        try:
            resp = self._garanti_requests(
                "POST",
                self.provider._garanti_get_api_url(),
                params=vals,
            )
            return self._garanti_parse_response_html(resp)
        except Exception as e:
            raise ValidationError(
                _("Payment Error: An error occurred. Please try again.")
            )

    def _garanti_compute_security_data(self):
        return (
            sha1(
                (
                    self.provider.garanti_prov_password
                    + self.provider.garanti_terminal_id.zfill(9)
                ).encode("utf-8")
            )
            .hexdigest()
            .upper()
        )

    def _garanti_create_secure3d_hash(self):
        """Create secure3dhash for Garanti Sanal Pos API.

        :return: secure3dhash
        """
        hash_strings = (
            str(self.provider.garanti_terminal_id)
            + str(self.reference)  # terminalID
            + str(self.amount)  # orderid
            + str(self.provider._garanti_get_return_url())  # txnamount
            + str(self.provider._garanti_get_return_url())  # successurl
            + "sales"  # errorurl
            +  # txntype
            # "" +  # txninstallmentcount
            str(self.provider.garanti_store_key)
            + str(self._garanti_compute_security_data())  # storekey  # securitydata
        )
        # Save hash to transaction to use in callback.
        self.tx.garanti_secure3d_hash = (
            sha1(hash_strings.encode("utf-8")).hexdigest().upper()
        )
        return self.tx.garanti_secure3d_hash

    def _garanti_get_partner_lang(self):
        if self.tx.partner_id.lang and self.tx.partner_id.lang in ["tr_TR", "tr"]:
            return "tr"
        else:
            return "en"

    def _garanti_create_payment_vals(self):
        """Create parameters for Garanti Sanal Pos API.

        :return: Parameters
        """
        return {
            "refreshtime": "0",
            "paymenttype": "creditcard",
            "secure3dsecuritylevel": "3D",
            "txntype": "sales",
            "cardname": self.card_args.get("card_name"),
            "cardnumber": self.provider._garanti_format_card_number(
                self.card_args.get("card_number")
            ),
            "cardexpiredatemonth": self.card_args.get("card_valid_month"),
            "cardexpiredateyear": self.card_args.get("card_valid_year").replace(
                "20", ""
            ),
            "cardcvv2": self.card_args.get("card_cvv"),
            "companyname": self.provider._garanti_get_company_name(),
            "apiversion": "12",
            "mode": self.provider._garanti_get_mode(),
            "terminalprovuserid": self.provider.garanti_prov_user,
            "terminaluserid": self.provider.garanti_terminal_id,
            "terminalid": self.provider.garanti_terminal_id,
            "terminalmerchantid": self.provider.garanti_merchant_id,
            "orderid": self.reference,
            "customeremailaddress": self._get_partner_email(),
            "customeripaddress": self.client_ip,
            "txnamount": str(self.amount),
            "txncurrencycode": self.provider._garanti_get_currency_code(
                self.currency_id, self.tx
            ),
            "txninstallmentcount": "",  # Taksit yok. Boş olacak.
            "successurl": self.provider._garanti_get_return_url(),
            "errorurl": self.provider._garanti_get_return_url(),
            "lang": self._garanti_get_partner_lang(),
            "txntimestamp": round(time.time() * 1000),
            "txntimeoutperiod": 60,
            "addcampaigninstallment": "N",
            "totalinstallmentcount": "0",
            "installmentonlyforcommercialcard": "N",
            "secure3dhash": self._garanti_create_secure3d_hash(),
        }

    def _garanti_compute_callback_hash_data(self):
        """Create hash data for Garanti Sanal Pos API.

        :return: Hash data
        """
        hash_data = (
            str(self.notification_data.get("oid"))
            + str(self.notification_data.get("clientid"))  # orderid
            + str(self.notification_data.get("txnamount"))  # clientid
            +  # txnamount
            # str(self.notification_data.get('txncurrencycode')) +  # txncurrencycode
            str(self._garanti_compute_security_data())  # securitydata
        )
        return sha1(hash_data.encode("utf-8")).hexdigest().upper()

    def _garanti_terminal_node(self, gvps_request):
        """Create terminal node for Garanti Sanal Pos API.

        :param gvps_request: GVPSRequest
        :return: Terminal node
        """
        terminal = etree.SubElement(gvps_request, "Terminal")
        etree.SubElement(terminal, "ProvUserID").text = self.notification_data.get(
            "terminalprovuserid"
        )
        etree.SubElement(
            terminal, "HashData"
        ).text = self._garanti_compute_callback_hash_data()
        etree.SubElement(terminal, "UserID").text = self.notification_data.get(
            "terminaluserid"
        )
        etree.SubElement(terminal, "ID").text = self.notification_data.get("clientid")
        etree.SubElement(terminal, "MerchantID").text = self.notification_data.get(
            "terminalmerchantid"
        )
        return True

    def _garanti_customer_node(self, gvps_request):
        """Create customer node for Garanti Sanal Pos API.

        :param gvps_request: GVPSRequest
        :return: Customer node
        """
        customer = etree.SubElement(gvps_request, "Customer")
        etree.SubElement(customer, "IPAddress").text = self.notification_data.get(
            "customeripaddress"
        )
        etree.SubElement(customer, "EmailAddress").text = self.notification_data.get(
            "customeremailaddress"
        )
        return True

    def _garanti_card_node(self, gvps_request):
        """Create card node for Garanti Sanal Pos API.

        :param gvps_request: GVPSRequest
        :return: Card node
        """
        card = etree.SubElement(gvps_request, "Card")
        etree.SubElement(card, "Number").text = ""
        etree.SubElement(card, "ExpireDate").text = ""
        etree.SubElement(card, "CVV2").text = ""
        return True

    def _garanti_address_list_node(self, order_node):
        address_list = etree.SubElement(order_node, "AddressList")
        address = etree.SubElement(address_list, "Address")
        etree.SubElement(address, "Type").text = "B"
        etree.SubElement(address, "Name").text = ""
        etree.SubElement(address, "LastName").text = ""
        etree.SubElement(address, "Company").text = ""
        etree.SubElement(address, "Text").text = ""
        etree.SubElement(address, "District").text = ""
        etree.SubElement(address, "City").text = ""
        etree.SubElement(address, "PostalCode").text = ""
        etree.SubElement(address, "Country").text = ""
        etree.SubElement(address, "PhoneNumber").text = ""
        return True

    def _garanti_order_node(self, gvps_request):
        """
        Create order node for Garanti Sanal Pos API.

        :param gvps_request: GVPSRequest
        :return: Order node
        """
        order = etree.SubElement(gvps_request, "Order")
        etree.SubElement(order, "OrderID").text = self.notification_data.get("oid")
        etree.SubElement(order, "GroupID").text = ""
        self._garanti_address_list_node(order)
        return True

    def _garanti_transaction_node(self, gvps_request):
        """
        Create transaction node for Garanti Sanal Pos API.

        :return: Transaction node
        """
        transaction = etree.SubElement(gvps_request, "Transaction")
        etree.SubElement(transaction, "Type").text = self.notification_data.get(
            "txntype"
        )
        etree.SubElement(
            transaction, "InstallmentCnt"
        ).text = self.notification_data.get("txninstallmentcount")
        etree.SubElement(transaction, "Amount").text = self.notification_data.get(
            "txnamount"
        )
        etree.SubElement(transaction, "CurrencyCode").text = self.notification_data.get(
            "txncurrencycode"
        )
        etree.SubElement(transaction, "CardholderPresentCode").text = "13"
        etree.SubElement(transaction, "MotoInd").text = "N"

        secure3d = etree.SubElement(transaction, "Secure3D")
        etree.SubElement(
            secure3d, "AuthenticationCode"
        ).text = self.notification_data.get("cavv")
        etree.SubElement(secure3d, "SecurityLevel").text = self.notification_data.get(
            "eci"
        )
        etree.SubElement(secure3d, "TxnID").text = self.notification_data.get("xid")
        etree.SubElement(secure3d, "Md").text = self.notification_data.get("md")
        return True

    def _garanti_create_callback_xml(self):
        """Create XML for Garanti Sanal Pos API.

        :return: XML string
        """
        gvps_request = etree.Element("GVPSRequest")

        mode = etree.SubElement(gvps_request, "Mode")
        mode.text = self.provider._garanti_get_mode()

        version = etree.SubElement(gvps_request, "Version")
        version.text = "16"

        channel_code = etree.SubElement(gvps_request, "ChannelCode")
        channel_code.text = ""

        self._garanti_terminal_node(gvps_request)
        self._garanti_customer_node(gvps_request)
        self._garanti_card_node(gvps_request)
        self._garanti_order_node(gvps_request)
        self._garanti_transaction_node(gvps_request)

        return etree.tostring(
            gvps_request, encoding="UTF-8", xml_declaration=True, pretty_print=True
        )

    def _garanti_payment_callback(self, notification_data):
        """Send payment callback to Garanti Sanal Pos API.

        :return: Response
        """
        self.notification_data = notification_data
        xml_data = self._garanti_create_callback_xml()
        try:
            resp = self._garanti_requests(
                "POST",
                self.provider._garanti_get_prov_url(),
                data=xml_data.decode("utf-8"),
            )
        except Exception as e:
            return _("Payment Error: Please try again.")

        try:
            root = etree.fromstring(resp.content)
            reason_code = root.find(".//Transaction/Response/ReasonCode").text
            message = root.find(".//Transaction/Response/Message").text
            if reason_code != "00" or message != "Approved":
                return root.find(".//Transaction/Response/ErrorMsg").text
            else:
                return message
        except Exception:  # pylint: disable=broad-except
            return _("Payment Error: Please try again.")

    def _garanti_create_query_transaction_vals(self):
        """Create Provision XML for Garanti Sanal Pos API.

        :return: XML string
        """
        gvps_request = etree.Element("GVPSRequest")

        mode = etree.SubElement(gvps_request, "Mode")
        mode.text = self.provider._garanti_get_mode()

        version = etree.SubElement(gvps_request, "Version")
        version.text = "16"

        channel_code = etree.SubElement(gvps_request, "ChannelCode")
        channel_code.text = ""

        self._garanti_terminal_node(gvps_request)
        self._garanti_customer_node(gvps_request)
        self._garanti_card_node(gvps_request)
        self._garanti_order_node(gvps_request)
        self._garanti_transaction_node(gvps_request)

        return etree.tostring(
            gvps_request, encoding="UTF-8", xml_declaration=True, pretty_print=True
        )

    def _garanti_query_transaction(self):
        """Send query transaction to Garanti Sanal Pos API.

        :return: Response
        """
        self.notification_data = {
            "oid": self.reference,
            "clientid": self.provider.garanti_terminal_id,
            "txnamount": str(self.amount),
            "txncurrencycode": self.provider._garanti_get_currency_code(
                self.currency_id, self.tx
            ),
            "terminalprovuserid": self.provider.garanti_prov_user,
            "terminaluserid": self.provider.garanti_terminal_id,
            "terminalmerchantid": self.provider.garanti_merchant_id,
            "txntype": "orderhistoryinq",
            "txninstallmentcount": "",
            "customeripaddress": "127.0.0.1",
            "customeremailaddress": self._get_partner_email(),
        }
        xml_data = self._garanti_create_query_transaction_vals()
        try:
            resp = self._garanti_requests(
                "POST",
                self.provider._garanti_get_prov_url(),
                data=xml_data.decode("utf-8"),
            )
        except Exception as e:
            raise ValidationError(
                _("Payment Error: An error occurred. Please try again.")
            )
        root = etree.fromstring(resp.content)
        reason_code = root.find(".//ReasonCode")
        error_msg = root.find(".//ErrorMsg")
        if reason_code.text and error_msg.text:
            return error_msg.text
        return True
