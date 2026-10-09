"""Explain existing pressure-condition calculations; never alter verdicts."""
import math


def append_final_pressure_explanations(result):
    for sample in result.get('samples') or []:
        for check in sample.get('checks') or []:
            trace=(check.get('pressure_calculation') or {}).get('text')
            if trace and '【高温压力计算过程】' not in str(check.get('note') or ''):
                check['note']=str(check.get('note') or '')+'\n【高温压力计算过程】\n'+trace
    return result


def explain_pressure(inputs, checks, profile):
    fmt=lambda v:format(v,'.8g') if isinstance(v,(int,float)) and math.isfinite(v) else '未确认'
    by_field={c['field']:c for c in checks}
    lines=[]
    evidence=inputs.get('dimension_evidence') or []
    for e in evidence:
        values=[]
        if e.get('measured_axes_mm'):values.append('外形尺寸'+'×'.join(fmt(v) for v in e['measured_axes_mm'])+' mm')
        if e.get('sheath_mean_thickness_mm') is not None:values.append('护套平均厚度'+fmt(e['sheath_mean_thickness_mm'])+' mm')
        lines.append('取值来源：PDF第'+str(e.get('page','未确认'))+'页同一样品结构表，'+'，'.join(values)+'。')
    d=inputs.get('diameter_for_force_mm');t=inputs.get('sheath_mean_thickness_mm')
    k=inputs.get('force_coefficient');force=inputs.get('expected_force_n')
    lines.append('计算用途：结构表尺寸交叉核对；尚未证明这些尺寸就是高温压力试样的原始测量记录。'
                 if not inputs.get('specimen_identity_verified') else '尺寸已确认属于本次压力试样。')
    lines.append('D取值：'+fmt(d)+' mm；'+inputs.get('diameter_selection_reason','选取依据未确认')+'。')
    lines.append('系数k：'+fmt(k)+'；'+inputs.get('coefficient_reason','系数依据未确认')+'。')
    lines.append('荷载公式：F = k × √(2Dδ − δ²)，D、δ以mm计，F以N计。')
    valid=all(type(v) in (int,float) and math.isfinite(v) for v in (d,t,k,force))
    if valid and 0<t<d and k>0:
        calculated=k*math.sqrt(2*d*t-t*t)
        if not math.isclose(calculated,force,rel_tol=1e-10,abs_tol=1e-10):
            lines.append('计算记录不一致：输入代入值与记录结果不同，不能展示为已核实计算。')
        else:
            lines.append(f'代入：F = {fmt(k)} × √(2 × {fmt(d)} × {fmt(t)} − {fmt(t)}²) = {fmt(force)} N。')
            lines.append(f'向下化整校核：0.97F = {fmt(.97*force)} N；按这些输入计算的未修约校核区间为[{fmt(.97*force)}, {fmt(force)}] N。')
            c=by_field.get('荷载',{});reported=c.get('reported')
            if type(reported) in (int,float) and math.isfinite(reported):
                lines.append(f'报告荷载：{c.get("reported_text") or fmt(reported)} N；与计算值相差{fmt(reported-force)} N（{fmt((reported/force-1)*100)}%）。')
                if c.get('reason')=='reported_display_matches_calculation':
                    lines.append('修约说明：按报告小数位显示后与计算值一致，仅说明报告显示相容，不证明设备实际加荷；不扩大向下化整3%的范围。')
                if c.get('review_origin')=='pressure_specimen_input_binding':
                    lines.append('差异处理：先核对压力试样实际D、δ和设备荷载记录，不凭结构表交叉计算直接判不合格。')
    else:
        lines.append('输入未齐全或无效，暂不代入计算；不补猜尺寸或系数。')
    time=by_field.get('时间',{});temp=by_field.get('温度',{});depth=by_field.get('压痕深度',{})
    lines.append('时间选择：'+inputs.get('time_selection_reason','尺寸条件未唯一确认')+'；报告'+fmt(time.get('reported'))+' h，要求'+str(time.get('required','未确认'))+'。')
    lines.append('温度选择：'+str(profile.get('basis','适用产品/材料依据未确认'))+'；报告'+fmt(temp.get('reported'))+' ℃，要求'+str(temp.get('required','未确认'))+'。')
    if depth:
        lines.append('压痕结果：'+fmt(depth.get('reported'))+'%；要求'+str(depth.get('required','未确认'))+'。该百分比是报告值，本段未从原始压痕深度和厚度另行复算。')
    return {'formula':'F = k × √(2Dδ − δ²)','steps':lines,'text':'\n'.join(lines),
            'scope':'explanation_only_existing_verdict_unchanged'}
