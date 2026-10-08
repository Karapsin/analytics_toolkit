with source as (
	select
		period_type,
		period_start,
		period_end,
		exp_name,
		test_group,
		n_test,
		n_control,
		arpu_test,
		arpu_control,
		arpu_cuped_test,
		arpu_cuped_control,
		uplift_cuped_pct,
		p_value_cuped,
		accepts_pct,
		utils_pct,
		discounts,
		bonuses_accrued,
		bonuses_spent,
		gross_margin_test,
		gross_margin_control,
		revenue_without_nds_test,
		revenue_without_nds_control,
		n_acceptors,
		n_utilizers,
		n_bonus_recipients,
		n_discount_recipients,
		n_reward_recipients,
		n_both_recipients,
		updated_at,
		loaded_at,
		count(*) over (partition by period_type, period_start, period_end, exp_name) as comparison_count
	from __TABLE_MONTH__
	where 1=1
),

comparisons as (
	select
		period_type,
		period_start,
		period_end,
		exp_name,
		test_group,
		test_group as comparison_group,
		n_test,
		n_control,
		arpu_test,
		arpu_control,
		arpu_cuped_test,
		arpu_cuped_control,
		uplift_cuped_pct,
		p_value_cuped,
		accepts_pct,
		utils_pct,
		discounts,
		bonuses_accrued,
		bonuses_spent,
		gross_margin_test,
		gross_margin_control,
		revenue_without_nds_test,
		revenue_without_nds_control,
		n_acceptors,
		n_utilizers,
		n_bonus_recipients,
		n_discount_recipients,
		n_reward_recipients,
		n_both_recipients,
		updated_at,
		loaded_at
	from source
	where 1=1
		and test_group <> 'TOTAL_TEST'

	union all

	select
		period_type,
		period_start,
		period_end,
		exp_name,
		test_group,
		'Все тестовые группы' as comparison_group,
		n_test,
		n_control,
		arpu_test,
		arpu_control,
		arpu_cuped_test,
		arpu_cuped_control,
		uplift_cuped_pct,
		p_value_cuped,
		accepts_pct,
		utils_pct,
		discounts,
		bonuses_accrued,
		bonuses_spent,
		gross_margin_test,
		gross_margin_control,
		revenue_without_nds_test,
		revenue_without_nds_control,
		n_acceptors,
		n_utilizers,
		n_bonus_recipients,
		n_discount_recipients,
		n_reward_recipients,
		n_both_recipients,
		updated_at,
		loaded_at
	from source
	where 1=1
		and (test_group = 'TOTAL_TEST' or comparison_count = 1)
)

select
	period_type,
	period_start,
	subtractDays(period_end, 1) as period_last_day,
	if(
		period_type = 'month',
		formatDateTime(period_start, '%m.%Y'),
		concat(formatDateTime(period_start, '%d.%m.%Y'), ' — ', formatDateTime(subtractDays(period_end, 1), '%d.%m.%Y'))
	) as period_label,
	case
		when period_type = 'month' then formatDateTime(toStartOfMonth(period_start), '%m.%Y')
		when period_type = 'week' then concat(
			formatDateTime(toMonday(period_start), '%d.%m.%Y'), ' — ',
			formatDateTime(addDays(toMonday(period_start), 6), '%d.%m.%Y')
		)
		else 'Всё время'
	end as summary_period_label,
	exp_name,
	comparison_group,
	test_group,
	n_test,
	n_control,
	n_acceptors,
	n_utilizers,
	n_bonus_recipients,
	n_discount_recipients,
	n_reward_recipients,
	n_both_recipients,
	accepts_pct,
	utils_pct,
	toFloat64(n_utilizers) / nullIf(toFloat64(n_acceptors), 0) as util_accept_rate,
	bonuses_accrued,
	bonuses_spent,
	discounts,
	bonuses_spent + discounts as costs,
	arpu_test,
	arpu_control,
	arpu_cuped_test,
	arpu_cuped_control,
	arpu_test - arpu_control as uplift_raw,
	arpu_test / nullIf(arpu_control, 0) - 1 as uplift_raw_pct,
	arpu_cuped_test - arpu_cuped_control as uplift_cuped,
	uplift_cuped_pct,
	p_value_cuped,
	case
		when p_value_cuped is null then 'Нет оценки'
		when p_value_cuped < 0.0001 then '<0,0001'
		else replaceAll(toString(round(p_value_cuped, 4)), '.', ',')
	end as p_value_label,
	case
		when p_value_cuped is null then 'Нет оценки'
		when p_value_cuped < 0.05 then 'Да'
		else 'Нет'
	end as significant,
	(arpu_test - arpu_control) * n_test as additional_rto_raw,
	(arpu_cuped_test - arpu_cuped_control) * n_test as additional_rto_cuped,
	arpu_test * n_test as revenue_test,
	arpu_control * n_control as revenue_control,
	revenue_without_nds_test,
	revenue_without_nds_control,
	gross_margin_test,
	gross_margin_control,
	gross_margin_test / nullIf(arpu_test * n_test, 0) as margin_test_pct,
	gross_margin_control / nullIf(arpu_control * n_control, 0) as margin_control_pct,
	gross_margin_test / nullIf(revenue_without_nds_test, 0) as margin_without_nds_test_pct,
	gross_margin_control / nullIf(revenue_without_nds_control, 0) as margin_without_nds_control_pct,
	gross_margin_test - gross_margin_control * n_test / nullIf(n_control, 0) as additional_margin,
	gross_margin_test - gross_margin_control * n_test / nullIf(n_control, 0) - bonuses_spent - discounts as additional_margin_net,
	(gross_margin_test - gross_margin_control * n_test / nullIf(n_control, 0) - bonuses_spent - discounts)
		/ nullIf(bonuses_spent + discounts, 0) as roi,
	formatDateTime(updated_at, '%d.%m.%Y %H:%i:%S', 'Europe/Moscow') as updated_label,
	formatDateTime(loaded_at, '%d.%m.%Y %H:%i:%S', 'Europe/Moscow') as loaded_label
from comparisons
