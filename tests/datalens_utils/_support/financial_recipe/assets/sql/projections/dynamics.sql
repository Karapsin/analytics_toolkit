with financial_source as (
	select
		exp_name,
		test_group,
		period_type,
		period_start,
		toNullable(p_value_cuped) as p_value_cuped,
		count(*) over (partition by exp_name, period_type, period_start) as comparison_count
	from __TABLE_MONTH__
	where 1=1
		and period_type in ('week', 'month')
),

significance as (
	select
		exp_name,
		test_group,
		period_type,
		period_start,
		p_value_cuped
	from financial_source f
	where 1=1
		and f.test_group <> 'TOTAL_TEST'

	union all

	select
		exp_name,
		'Все тестовые группы' as test_group,
		period_type,
		period_start,
		p_value_cuped
	from financial_source f
	where 1=1
		and (f.test_group = 'TOTAL_TEST' or f.comparison_count = 1)
)

select
	d.exp_name,
	d.test_group,
	d.period_type,
	d.period_start,
	d.period_end,
	d.n_test,
	d.n_acceptors,
	d.n_utilizers,
	d.n_acceptors_cumulative,
	d.n_utilizers_cumulative,
	d.n_bonus_recipients,
	d.n_discount_recipients,
	d.bonuses_accrued,
	d.bonuses_spent,
	d.discounts,
	d.revenue_test,
	d.revenue_without_nds_test,
	d.gross_margin_test,
	d.n_control,
	d.revenue_control,
	d.revenue_without_nds_control,
	d.gross_margin_control,
	d.n_bonus_recipients_cumulative,
	d.n_discount_recipients_cumulative,
	d.bonuses_accrued_cumulative,
	d.bonuses_spent_cumulative,
	d.discounts_cumulative,
	d.revenue_test_cumulative,
	d.revenue_control_cumulative,
	d.revenue_without_nds_test_cumulative,
	d.revenue_without_nds_control_cumulative,
	d.gross_margin_test_cumulative,
	d.gross_margin_control_cumulative,
	s.p_value_cuped
from __TABLE_DYNAMICS__ d
left join significance s
	on d.exp_name = s.exp_name
	and d.test_group = s.test_group
	and d.period_type = s.period_type
	and d.period_start = s.period_start
where 1=1
