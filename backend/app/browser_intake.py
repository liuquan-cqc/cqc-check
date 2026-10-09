"""Typed browser intake. No external model/OCR calls; uncertain identity fails closed."""
import hashlib
import re
import threading
from functools import lru_cache
from pathlib import Path
from sqlalchemy import Column, Integer, String, ForeignKey, UniqueConstraint, text
from backend.app.database import Base
from backend.app import models
from fastapi import HTTPException


class BrowserReportScope(Base):
    __tablename__ = 'browser_report_scopes'
    id = Column(Integer, primary_key=True)
    application_no = Column(String(100), nullable=False)
    task_no = Column(String(100), nullable=False)
    category = Column(String(40), nullable=False)
    __table_args__ = (UniqueConstraint('application_no', 'task_no', 'category', name='uq_browser_report_scope'),)


class BrowserReportBinding(Base):
    __tablename__ = 'browser_report_bindings'
    report_id = Column(Integer, ForeignKey('reports.id', ondelete='CASCADE'), primary_key=True)
    scope_id = Column(Integer, ForeignKey('browser_report_scopes.id', ondelete='CASCADE'), nullable=False, index=True)
    evidence = Column(String(300), nullable=False)


LABELS = {'type_test':'总型式试验报告', 'arsenic':'砷元素快检', 'spectrum':'图谱检查'}
APP_RE = r'A\d{4}CCC\d{4}-\d+'
TASK_RE = r'T\d{4}CCC-[A-Z]-\d+'
_OCR_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _cover_ocr():
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)


def pdf_pages(doc):
    pages=[doc[i].get_text('text') for i in range(min(4,len(doc)))]
    if len(re.sub(r'\s+','',''.join(pages))) >= 30: return pages
    # Only cover/identity pages, using bundled local OCR. No network or API key.
    import fitz
    import cv2
    import numpy as np
    with _OCR_LOCK:
        engine=_cover_ocr()
        for i in range(min(4,len(doc))):
            scale=min(1.7,2048/max(doc[i].rect.width,doc[i].rect.height))
            pix=doc[i].get_pixmap(matrix=fitz.Matrix(scale,scale),alpha=False)
            image=cv2.imdecode(np.frombuffer(pix.tobytes('png'),dtype=np.uint8),cv2.IMREAD_COLOR)
            entries,_=engine(image)
            pages[i]='\n'.join(str(entry[1]) for entry in (entries or []) if float(entry[2])>=0.8)
            if i >= 1:
                try:
                    classify_pages(pages[:i+1])
                    break
                except HTTPException:
                    pass
    return pages


def uncertain(code, message):
    raise HTTPException(409, detail={'code': code, 'message': message})


def classify_pages(pages):
    lines = [re.sub(r'\s+', '', line) for page in pages for line in page.splitlines() if line.strip()]
    compact = ''.join(lines)
    if len(compact) < 30:
        uncertain('category_unknown', 'PDF文字层不足，暂不能自动判断类别；未创建审核任务')
    # Category evidence must come from a standalone title, not incidental body references.
    patterns = {
        'type_test': r'(?:总|安全)?型式试验(?:检测)?报告(?:书)?',
        'arsenic': r'砷元素(?:快检)?(?:分析|检测|测试|分析测试)?报告(?:书)?',
        'spectrum': r'(?:红外(?:光谱)?|图谱|光谱|材料(?:一致性)?)(?:分析|检测|检查|比对|测试)?报告(?:书)?',
    }
    found = {k for k,p in patterns.items() if any(re.fullmatch(p,line) for line in lines)}
    fee_title = any(re.fullmatch(r'(?:检测)?收费(?:通知)?单|发票|缴费通知单|付款通知书',line) for line in lines)
    if fee_title:
        if found: uncertain('category_ambiguous','文件同时包含报告与收费附件标题，需核对是否为合并文件')
        return {'category':'non_report','evidence':'文件内容标明收费/付款附件','pages_text':compact}
    # Cover titles are sometimes split over lines by the PDF text extractor.
    first = re.sub(r'\s+', '', pages[0])[:900] if pages else ''
    if not found:
        for k,p in patterns.items():
            if re.search(p,first): found.add(k)
    if not found and '砷元素快检分析测试结果' in compact and '砷元素分析报告' in compact:
        found.add('arsenic')
    if not found and re.search(r'试验依据标准.{0,500}6040[-—－]2019.{0,300}33047[.]1[-—－]2016',compact) and re.search(r'试验结论.{0,100}红外光谱.{0,80}热重',compact):
        found.add('spectrum')
    conventional=bool(re.search(r'导体电阻|高温压力|老化前抗张|成品(?:电缆|电线电缆)?电压试验',compact))
    if not conventional:
        if '砷元素分析报告' in compact and '砷元素快检分析测试结果' in compact:
            found.discard('type_test'); found.add('arsenic')
        elif re.search(r'试验依据标准.{0,500}6040[-—－]2019.{0,300}33047[.]1[-—－]2016',compact) and re.search(r'试验结论.{0,100}红外光谱.{0,80}热重.{0,100}(?:砷|锑).{0,100}见附表',compact):
            found.discard('type_test'); found.add('spectrum')
    if not found and any(re.fullmatch(r'(?:检测)?收费(?:通知)?单|发票|缴费通知单|付款通知书',line) for line in lines):
        return {'category':'non_report','evidence':'文件内容标明收费/付款附件','pages_text':compact}
    if len(found)!=1:
        uncertain('category_ambiguous' if found else 'category_unknown', '报告类别不明确或包含多个类别标题；未自动归类，请核对文件')
    category=next(iter(found))
    if category in ('arsenic','spectrum') and re.search(r'导体电阻|高温压力|老化前抗张|成品(?:电缆|电线电缆)?电压试验',compact):
        uncertain('mixed_report_scope','专项标题与常规试验内容同时出现，需核对是否为合并报告')
    scope_supported = category == 'type_test'
    if category == 'arsenic': scope_supported = '砷元素分析报告' in compact and '砷元素快检分析测试结果' in compact
    if category == 'spectrum':
        scope_supported = bool(re.search(r'试验依据标准.{0,500}6040[-—－]2019.{0,300}33047[.]1[-—－]2016',compact)
            and re.search(r'试验结论.{0,100}红外光谱.{0,80}热重.{0,100}(?:砷|锑).{0,100}见附表',compact))
    return {'category':category, 'evidence':'PDF内容类别：'+LABELS[category], 'pages_text':compact, 'scope_supported':scope_supported}


def read_identity(file, app_no, task_no):
    import fitz
    if not re.fullmatch(APP_RE,app_no) or not re.fullmatch(TASK_RE,task_no):
        uncertain('source_identity_missing','来源申请编号或试验任务编号缺失/无效')
    file.file.seek(0)
    data=file.file.read()
    file.file.seek(0)
    try:
        with fitz.open(stream=data,filetype='pdf') as doc:
            if doc.needs_pass: uncertain('encrypted_pdf','PDF已加密，无法自动判断类别')
            try:
                pages=pdf_pages(doc)
            except HTTPException: raise
            except Exception: uncertain('cover_ocr_failed','本地封面识别未成功，暂不能自动归类；可重试，未创建审核任务')
            result=classify_pages(pages)
    except HTTPException: raise
    except Exception: uncertain('invalid_pdf','文件不是可读取的PDF；未创建审核任务')
    content=result.pop('pages_text')
    apps=set(re.findall(APP_RE,content)); tasks=set(re.findall(TASK_RE,content))
    if apps and apps!={app_no}: uncertain('application_mismatch','PDF内申请编号与来源业务行不一致')
    if tasks and tasks!={task_no}: uncertain('task_mismatch','PDF内试验任务编号与来源业务行不一致')
    return result


def _legacy_task(db, report):
    # Old browser audit records have exact report IDs and source task numbers.
    candidates=set()
    for row in db.query(models.AuditLog).filter(models.AuditLog.module=='浏览器同步', models.AuditLog.action=='同步单位系统报告').all():
        detail=row.detail or ''
        if re.search(r'报告ID[：:]\s*'+str(report.id)+r'(?!\d)',detail):
            candidates.update(re.findall(TASK_RE,detail))
    return next(iter(candidates)) if len(candidates)==1 else ''


def guard_existing_scope(db, report, file=None):
    binding=db.get(BrowserReportBinding,report.id)
    if not binding: return
    scope=db.get(BrowserReportScope,binding.scope_id)
    if file is None:
        with open(report.file_path,'rb') as stream:
            identity=read_identity(type('StoredPDF',(),{'file':stream})(),scope.application_no,scope.task_no)
    else:
        identity=read_identity(file,scope.application_no,scope.task_no)
    if identity['category'] != scope.category:
        raise HTTPException(409, detail={
            'code':'correction_category_mismatch',
            'message':'更正文件类别与原报告不同，不能加入原版本链，可作为独立类别上传',
            'report_id':report.id,
            'application_no':scope.application_no,
            'task_no':scope.task_no,
            'original_category':scope.category,
            'original_category_label':LABELS.get(scope.category,scope.category),
            'detected_category':identity['category'],
            'detected_category_label':LABELS.get(identity['category'],identity['category']),
        })
    if not identity.get('scope_supported'):
        uncertain('special_scope_unconfirmed','报告已保存，但专项自动审核范围尚未确认，不能通过重新审核套用型式试验矩阵')


def create_independent_category(request, file, source_report, db, current):
    """把类别不一致的更正文件放入同申请、同任务下的独立类别版本链。"""
    from backend.app.routers import reports
    from backend.app.config import get_settings
    from backend.app.settings_store import get_section
    from backend.app.progress import write_progress
    from backend.app.audit import add_audit

    binding=db.get(BrowserReportBinding,source_report.id)
    if not binding:
        uncertain('independent_category_scope_missing','原报告缺少可验证的申请、任务和类别归属，不能自动创建独立类别')
    source_scope=db.get(BrowserReportScope,binding.scope_id)
    if not source_scope:
        uncertain('independent_category_scope_missing','原报告类别归属已不存在，不能自动创建独立类别')

    reports._validate_upload(file,db)
    identity=read_identity(file,source_scope.application_no,source_scope.task_no)
    category=identity['category']
    if category == 'non_report':
        uncertain('independent_category_non_report','文件内容属于收费或付款附件，不能创建报告')
    if category == source_scope.category:
        uncertain('correction_category_same','文件类别与原报告相同，请作为更正版本上传')

    digest=reports._upload_sha256(file)
    created_path=None
    try:
        with reports._UPLOAD_LOCK:
            if db.get_bind().dialect.name=='postgresql':
                lock_value=f'{source_scope.application_no}|{source_scope.task_no}|{category}'
                key=int.from_bytes(hashlib.sha256(lock_value.encode()).digest()[:8],'big',signed=True)
                db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':key})
            target_scope=db.query(BrowserReportScope).filter_by(
                application_no=source_scope.application_no,
                task_no=source_scope.task_no,
                category=category,
            ).first()
            if not target_scope:
                target_scope=BrowserReportScope(
                    application_no=source_scope.application_no,
                    task_no=source_scope.task_no,
                    category=category,
                )
                db.add(target_scope); db.flush()
            bound=db.query(models.Report).join(
                BrowserReportBinding,BrowserReportBinding.report_id==models.Report.id
            ).filter(BrowserReportBinding.scope_id==target_scope.id).all()
            exact=next((row for row in bound if row.file_sha256==digest),None)
            if exact:
                raise HTTPException(409, detail=reports._duplicate_detail(exact))
            parent=max(bound,key=lambda row:(row.revision_no or 1,row.id)) if bound else None
            report=models.Report(
                application_no=source_scope.application_no,
                status='pending',file_path='',ocr_path='',
                company_id=source_report.company_id,
                original_filename=reports._original_filename(file),file_sha256=digest,
                revision_no=(parent.revision_no or 1)+1 if parent else 1,
                root_report_id=(parent.root_report_id or parent.id) if parent else None,
                parent_report_id=parent.id if parent else None,
                revision_note=(
                    f'独立类别上传：{source_scope.task_no}；{LABELS[category]}；'
                    + ('内容变化，新建更正版本' if parent else '独立报告')
                ),
                product_desc=LABELS[category],uploaded_by_id=current.id,
            )
            db.add(report); db.flush()
            if not parent: report.root_report_id=report.id
            report.file_path=reports._save_upload(file,report.id); created_path=report.file_path
            metadata=reports._extract_upload_metadata(report.file_path)
            metadata['application_no']=source_scope.application_no
            reports._apply_metadata(db,report,metadata)
            db.add(BrowserReportBinding(
                report_id=report.id,scope_id=target_scope.id,
                evidence=identity['evidence']+'；由类别冲突确认入口独立上传',
            ))
            ocr_dir=Path(get_settings().ocr_dir)/f'report_{report.id}'
            ocr_dir.mkdir(parents=True,exist_ok=True); report.ocr_path=str(ocr_dir)
            write_progress(ocr_dir,'queued',0,0,f'{LABELS[category]} V{report.revision_no} 已接收')
            review_queued=bool(identity.get('scope_supported') and get_section('workflow',db).get('auto_review',True))
            if review_queued:
                db.add(models.Task(report_id=report.id,status='queued',attempt=0))
            elif not identity.get('scope_supported'):
                report.status='failed'
                report.error_msg='报告已独立归类，但专项自动审核范围尚未可靠确认；未启动自动审核，请人工核对'
                write_progress(ocr_dir,'failed',0,0,report.error_msg)
            add_audit(
                db,current,'报告','独立类别上传',
                f'来源报告ID：{source_report.id}，任务：{source_scope.task_no}，申请：{source_scope.application_no}，'
                f'类别：{category}，报告ID：{report.id}',request,
            )
            db.commit(); db.refresh(report)
            return report
    except Exception:
        db.rollback()
        if created_path: reports._safe_remove_file(created_path,get_settings().uploads_dir)
        raise


def intake(request, file, app_no, task_no, source_url, db, device):
    from backend.app.routers import reports
    from backend.app.config import get_settings
    from backend.app.settings_store import get_section
    from backend.app.progress import write_progress
    from backend.app.audit import add_audit
    creator=db.query(models.User).filter(models.User.id==device.created_by).first()
    if not creator or creator.status!='active': raise HTTPException(403,'配对管理员账号已停用')
    reports._validate_upload(file,db)
    identity=read_identity(file,app_no,task_no)
    if identity['category']=='non_report':
        return {'action':'skipped','message':'已跳过内容明确的收费/付款附件','category':'non_report'}
    category=identity['category']; digest=reports._upload_sha256(file)
    created_path=None
    try:
        with reports._UPLOAD_LOCK:
            if db.get_bind().dialect.name=='postgresql':
                key=int.from_bytes(hashlib.sha256(app_no.encode()).digest()[:8],'big',signed=True)
                db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':key})
            scope=db.query(BrowserReportScope).filter_by(application_no=app_no,task_no=task_no,category=category).first()
            if not scope:
                scope=BrowserReportScope(application_no=app_no,task_no=task_no,category=category); db.add(scope); db.flush()
            bound=db.query(models.Report).join(BrowserReportBinding,BrowserReportBinding.report_id==models.Report.id).filter(BrowserReportBinding.scope_id==scope.id).all()
            exact=next((r for r in bound if r.file_sha256==digest),None)
            if exact:
                db.commit()
                return {'action':'duplicate','report_id':exact.id,'category':category,'category_label':LABELS[category],'revision_no':exact.revision_no,'message':'同任务同类别文件已存在'}
            # Recover old records only with corroborated source task and PDF category.
            unbound=db.query(models.Report).outerjoin(BrowserReportBinding,BrowserReportBinding.report_id==models.Report.id).filter(models.Report.application_no==app_no,BrowserReportBinding.report_id==None).all()
            legacy=[]
            for old in unbound:
                old_task=_legacy_task(db,old)
                if old_task and old_task!=task_no: continue
                try:
                    with open(old.file_path,'rb') as stream:
                        old_id=read_identity(type('StoredPDF',(),{'file':stream})(),app_no,old_task or task_no)
                except (HTTPException, OSError):
                    uncertain('legacy_identity_unknown','同申请存在归属尚未确认的历史报告，需先核对，不能自动合并')
                if old_id['category']!=category: continue
                if not old_task:
                    uncertain('legacy_task_unknown','同类别历史报告缺少可验证的试验任务归属，需先核对一次')
                legacy.append(old)
            if legacy:
                roots={r.root_report_id or r.id for r in legacy}
                if len(roots)!=1: uncertain('legacy_multiple_chains','历史报告存在多条版本链，需先核对归属')
                chain=db.query(models.Report).filter(models.Report.root_report_id==next(iter(roots))).all()
                if {r.id for r in chain}!={r.id for r in legacy}:
                    uncertain('legacy_mixed_chain','历史版本链可能混有其他任务或类别，未改写旧记录')
                if bound: uncertain('legacy_multiple_chains','历史报告与新归属链并存，需先核对')
                for old in legacy:
                    db.add(BrowserReportBinding(report_id=old.id,scope_id=scope.id,evidence='历史来源任务审计＋PDF内容核验'))
                bound=legacy
            exact=next((r for r in bound if r.file_sha256==digest),None)
            if exact:
                db.commit()
                return {'action':'duplicate','report_id':exact.id,'category':category,'category_label':LABELS[category],'revision_no':exact.revision_no,'message':'同任务同类别文件已存在'}
            parent=max(bound,key=lambda r:(r.revision_no or 1,r.id)) if bound else None
            report=models.Report(application_no=app_no,status='pending',file_path='',ocr_path='',uploaded_by_id=creator.id,
                original_filename=reports._original_filename(file),file_sha256=digest,revision_no=(parent.revision_no or 1)+1 if parent else 1,
                root_report_id=(parent.root_report_id or parent.id) if parent else None,parent_report_id=parent.id if parent else None,
                revision_note=f'自动归属：{task_no}；{LABELS[category]}；'+('内容变化，新建更正版本' if parent else '独立报告'),
                product_desc=LABELS[category])
            db.add(report); db.flush()
            if not parent: report.root_report_id=report.id
            report.file_path=reports._save_upload(file,report.id); created_path=report.file_path
            metadata=reports._extract_upload_metadata(report.file_path)
            metadata['application_no']=app_no
            reports._apply_metadata(db,report,metadata)
            db.add(BrowserReportBinding(report_id=report.id,scope_id=scope.id,evidence=identity['evidence']))
            ocr_dir=Path(get_settings().ocr_dir)/f'report_{report.id}'
            ocr_dir.mkdir(parents=True,exist_ok=True); report.ocr_path=str(ocr_dir)
            write_progress(ocr_dir,'queued',0,0,f'{LABELS[category]} V{report.revision_no} 已接收')
            review_queued = bool(identity.get('scope_supported') and get_section('workflow',db).get('auto_review',True))
            if review_queued: db.add(models.Task(report_id=report.id,status='queued',attempt=0))
            elif not identity.get('scope_supported'):
                report.status='failed'
                report.error_msg='报告已上传并归类，但专项自动审核范围尚未可靠确认；未套用型式试验矩阵，不能视为审核通过'
                write_progress(ocr_dir,'failed',0,0,report.error_msg)
            action='revision' if parent else 'created'
            add_audit(db,creator,'浏览器同步','同步单位系统报告',f'任务：{task_no}，申请：{app_no}，类别：{category}，结果：{action}，报告ID：{report.id}，来源：{source_url[:300]}',request)
            device.last_seen_at=models._utc_now(); db.commit(); db.refresh(report)
            return {'action':action,'report_id':report.id,'application_no':app_no,'task_no':task_no,'category':category,'category_label':LABELS[category],'revision_no':report.revision_no,'status':report.status,'review_queued':review_queued,'message':report.error_msg or ''}
    except Exception:
        db.rollback()
        if created_path: reports._safe_remove_file(created_path,get_settings().uploads_dir)
        raise
