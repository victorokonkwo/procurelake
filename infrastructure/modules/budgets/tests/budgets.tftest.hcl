mock_provider "aws" {}

variables {
  name              = "procurelake-monthly-cost"
  monthly_limit_usd = 50
  alert_emails      = ["alerts@example.com"]
}

run "monthly_usd_budget_with_actual_and_forecast_alerts" {
  command = apply

  assert {
    condition = (
      aws_budgets_budget.monthly_cost.limit_amount == "50.00" &&
      aws_budgets_budget.monthly_cost.limit_unit == "USD" &&
      aws_budgets_budget.monthly_cost.time_unit == "MONTHLY" &&
      aws_budgets_budget.monthly_cost.budget_type == "COST"
    )
    error_message = "Expected a USD 50.00 monthly cost budget."
  }
  assert {
    condition = toset([for n in aws_budgets_budget.monthly_cost.notification : "${n.notification_type}:${n.threshold}"]) == toset([
      "ACTUAL:50", "ACTUAL:80", "ACTUAL:100", "FORECASTED:100",
    ])
    error_message = "Expected alerts at 50/80/100% actual and 100% forecast."
  }
  assert {
    condition     = alltrue([for n in aws_budgets_budget.monthly_cost.notification : contains(n.subscriber_email_addresses, "alerts@example.com")])
    error_message = "Every notification must go to the configured recipients."
  }
}

run "rejects_invalid_email" {
  command = plan

  variables {
    alert_emails = ["not-an-email"]
  }
  expect_failures = [var.alert_emails]
}

run "rejects_non_positive_limit" {
  command = plan

  variables {
    monthly_limit_usd = 0
  }
  expect_failures = [var.monthly_limit_usd]
}
