output "budget_name" {
  description = "Name of the budget."
  value       = aws_budgets_budget.monthly_cost.name
}
