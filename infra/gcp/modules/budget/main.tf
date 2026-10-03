# A monthly budget on the project with alerts at 50%, 90% and 100% of actual spend and 100% of
# forecast, published to a Pub/Sub topic that the budget guard subscribes to. Budgets do not cap
# spending (cost data lags by hours); the quota ceiling, the per-submit caps and the reaper do not
# depend on them.

variable "project_id" {
  type = string
}

variable "project_number" {
  type = string
}

variable "billing_account" {
  type = string
}

variable "budget_usd" {
  type = number
}

resource "google_pubsub_topic" "budget" {
  project = var.project_id
  name    = "yapnr-budget"
  labels  = { yapnr = "1" }
}

resource "google_billing_budget" "monthly" {
  billing_account = var.billing_account
  display_name    = "yapnr experiments (${var.project_id})"

  budget_filter {
    projects               = ["projects/${var.project_number}"]
    calendar_period        = "MONTH"
    credit_types_treatment = "INCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(floor(var.budget_usd))
    }
  }

  dynamic "threshold_rules" {
    for_each = [0.5, 0.9, 1.0]
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }

  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  all_updates_rule {
    pubsub_topic   = google_pubsub_topic.budget.id
    schema_version = "1.0"
  }
}

output "topic_id" {
  value = google_pubsub_topic.budget.id
}
