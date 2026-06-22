"""All Wave Accounting GraphQL query/mutation strings — single source of truth.

Wave exposes ONE GraphQL endpoint (https://gql.waveapps.com/graphql/public) that
serves both queries and mutations. Variables are passed in the standard
GraphQL `variables` field of the request body.

connector.py imports these as named constants; raw query strings never appear
in the orchestrator (SOC).
"""

# ── Identity / user ────────────────────────────────────────────────────────

USER_QUERY = """
query {
  user {
    id
    defaultEmail
    firstName
    lastName
  }
}
""".strip()


# ── Businesses ─────────────────────────────────────────────────────────────

BUSINESS_LIST_QUERY = """
query ListBusinesses($page: Int!, $pageSize: Int!) {
  businesses(page: $page, pageSize: $pageSize) {
    pageInfo { currentPage totalPages totalCount }
    edges {
      node {
        id
        name
        timezone
        address {
          addressLine1
          addressLine2
          city
          postalCode
          country { code name }
        }
      }
    }
  }
}
""".strip()


BUSINESS_GET_QUERY = """
query GetBusiness($id: ID!) {
  business(id: $id) {
    id
    name
    timezone
    currency { code symbol }
    address {
      addressLine1
      addressLine2
      city
      postalCode
      country { code name }
    }
  }
}
""".strip()


# ── Customers ──────────────────────────────────────────────────────────────

CUSTOMER_LIST_QUERY = """
query ListCustomers($businessId: ID!, $page: Int!, $pageSize: Int!) {
  business(id: $businessId) {
    id
    customers(page: $page, pageSize: $pageSize) {
      pageInfo { currentPage totalPages totalCount }
      edges {
        node {
          id
          name
          email
          firstName
          lastName
          mobile
          phone
        }
      }
    }
  }
}
""".strip()


CUSTOMER_GET_QUERY = """
query GetCustomer($businessId: ID!, $id: ID!) {
  business(id: $businessId) {
    id
    customer(id: $id) {
      id
      name
      email
      firstName
      lastName
      mobile
      phone
      address {
        addressLine1
        addressLine2
        city
        postalCode
        country { code name }
      }
    }
  }
}
""".strip()


CUSTOMER_CREATE_MUTATION = """
mutation CustomerCreate($input: CustomerCreateInput!) {
  customerCreate(input: $input) {
    didSucceed
    inputErrors { path message code }
    customer {
      id
      name
      email
    }
  }
}
""".strip()


# ── Invoices ───────────────────────────────────────────────────────────────

INVOICE_LIST_QUERY = """
query ListInvoices($businessId: ID!, $page: Int!, $pageSize: Int!, $status: InvoiceStatus) {
  business(id: $businessId) {
    id
    invoices(page: $page, pageSize: $pageSize, status: $status) {
      pageInfo { currentPage totalPages totalCount }
      edges {
        node {
          id
          invoiceNumber
          status
          invoiceDate
          dueDate
          total { value currency { code } }
          customer { id name }
        }
      }
    }
  }
}
""".strip()


INVOICE_GET_QUERY = """
query GetInvoice($businessId: ID!, $id: ID!) {
  business(id: $businessId) {
    id
    invoice(id: $id) {
      id
      invoiceNumber
      status
      invoiceDate
      dueDate
      total { value currency { code } }
      customer { id name email }
      items {
        product { id name }
        description
        quantity
        unitPrice
      }
    }
  }
}
""".strip()


INVOICE_CREATE_MUTATION = """
mutation InvoiceCreate($input: InvoiceCreateInput!) {
  invoiceCreate(input: $input) {
    didSucceed
    inputErrors { path message code }
    invoice {
      id
      invoiceNumber
      status
      total { value currency { code } }
    }
  }
}
""".strip()


# ── Products ───────────────────────────────────────────────────────────────

PRODUCT_LIST_QUERY = """
query ListProducts($businessId: ID!, $page: Int!, $pageSize: Int!) {
  business(id: $businessId) {
    id
    products(page: $page, pageSize: $pageSize) {
      pageInfo { currentPage totalPages totalCount }
      edges {
        node {
          id
          name
          description
          unitPrice
          isSold
          isBought
        }
      }
    }
  }
}
""".strip()


PRODUCT_CREATE_MUTATION = """
mutation ProductCreate($input: ProductCreateInput!) {
  productCreate(input: $input) {
    didSucceed
    inputErrors { path message code }
    product {
      id
      name
      unitPrice
      description
    }
  }
}
""".strip()


# ── Chart of accounts ──────────────────────────────────────────────────────

ACCOUNT_LIST_QUERY = """
query ListAccounts($businessId: ID!) {
  business(id: $businessId) {
    id
    accounts {
      edges {
        node {
          id
          name
          type { name normalBalanceType }
          subtype { name }
          isArchived
        }
      }
    }
  }
}
""".strip()


# ── Transactions ───────────────────────────────────────────────────────────

TRANSACTION_LIST_QUERY = """
query ListTransactions($businessId: ID!, $page: Int!, $pageSize: Int!, $from: Date, $to: Date) {
  business(id: $businessId) {
    id
    transactions(page: $page, pageSize: $pageSize, from: $from, to: $to) {
      pageInfo { currentPage totalPages totalCount }
      edges {
        node {
          id
          description
          date
          amount { value currency { code } }
        }
      }
    }
  }
}
""".strip()


# ── Sales taxes ────────────────────────────────────────────────────────────

SALES_TAX_LIST_QUERY = """
query ListSalesTaxes($businessId: ID!) {
  business(id: $businessId) {
    id
    salesTaxes {
      edges {
        node {
          id
          name
          abbreviation
          description
          rate
          taxNumber
        }
      }
    }
  }
}
""".strip()


# ── GraphQL error parser ───────────────────────────────────────────────────


def parse_graphql_errors(payload: dict) -> str:
    """Concatenate GraphQL `errors[]` messages into a single human string.

    Wave returns `errors` even on HTTP 200 when the query is partially valid —
    the GraphQL spec contract. This helper is used by `client/http_client.py`
    to raise typed exceptions on those payloads.
    """
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not errors or not isinstance(errors, list):
        return ""
    return "; ".join(
        (e.get("message") or "unknown error") if isinstance(e, dict) else str(e)
        for e in errors
    )
