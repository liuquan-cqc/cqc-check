<template>
  <div class="detail-page">
    <!-- 初始加载骨架屏 -->
    <div v-if="pageLoading" class="ccc-card">
      <el-skeleton :rows="8" animated />
    </div>

    <!-- 加载失败（404 / 无权限）错误态 -->
    <div v-else-if="loadError" class="ccc-card">
      <el-result icon="error" :title="loadError" sub-title="请返回列表页重新选择报告">
        <template #extra>
          <el-button type="primary" @click="goBack">返回列表</el-button>
        </template>
      </el-result>
    </div>

    <template v-else-if="report">
      <!-- 顶部操作栏 -->
      <div class="ccc-card action-bar">
        <div class="action-left">
          <el-button :icon="Back" text @click="goBack">返回列表</el-button>
          <span class="report-no">{{ report.application_no || report.report_no || `申请 #${report.id}` }}</span>
          <StatusTag :status="report.status" />
        </div>
        <div class="action-right">
          <el-button
            v-permission="'reports.upload'"
            type="success"
            :disabled="report.status !== 'done' || !isLatestVersion"
            @click="openRevisionDialog"
          >
            上传更正版本
          </el-button>
          <el-button
            v-permission="'reports.review'"
            type="primary"
            plain
            :disabled="report.status !== 'done'"
            @click="openCorrection()"
          >
            提交纠错
          </el-button>
          <el-button
            v-permission="'reports.download'"
            :icon="Download"
            :disabled="report.status !== 'done' || trialNeedsFinal"
            @click="onDownload"
          >
            下载意见书
          </el-button>
          <el-button
            v-permission="'reports.review'"
            type="warning"
            :icon="RefreshRight"
            :loading="reReviewing"
            @click="onReReview"
          >
            重新审核
          </el-button>
          <el-button :icon="pdfCollapsed ? View : Hide" @click="pdfCollapsed = !pdfCollapsed">
            {{ pdfCollapsed ? '显示PDF' : '隐藏PDF' }}
          </el-button>
        </div>
      </div>

      <el-row :gutter="16">
        <!-- 左侧主内容区 -->
        <el-col :xs="24" :lg="pdfCollapsed ? 24 : 13">
          <!-- 报告基本信息 -->
          <div class="ccc-card info-card">
            <div class="card-title">报告信息</div>
            <el-descriptions :column="2" border>
              <el-descriptions-item label="申请编号">{{ report.application_no || '-' }}</el-descriptions-item>
              <el-descriptions-item label="企业名称">{{ report.company?.name || '-' }}</el-descriptions-item>
              <el-descriptions-item label="产品单元">{{ report.product_unit || '-' }}</el-descriptions-item>
              <el-descriptions-item label="报告编号">{{ report.report_no || '-' }}</el-descriptions-item>
              <el-descriptions-item label="上传时间">{{ formatTime(report.created_at) }}</el-descriptions-item>
              <el-descriptions-item label="审核完成时间">{{ formatTime(report.review_completed_at) }}</el-descriptions-item>
              <el-descriptions-item label="文件版本">
                <el-tag type="primary" effect="plain">V{{ report.revision_no || 1 }}</el-tag>
                <span v-if="report.parent_report_id" class="revision-inline">更正版</span>
              </el-descriptions-item>
              <el-descriptions-item label="更正说明">{{ report.revision_note || '首版报告' }}</el-descriptions-item>
              <el-descriptions-item label="产品描述" :span="2">{{ report.product_desc || '-' }}</el-descriptions-item>
            </el-descriptions>
          </div>

          <div v-if="versions.length > 1" class="ccc-card version-card">
            <div class="card-title">版本记录</div>
            <div class="version-timeline">
              <button
                v-for="item in versions"
                :key="item.id"
                class="version-node"
                :class="{ active: item.id === report.id }"
                type="button"
                @click="goVersion(item.id)"
              >
                <span class="version-badge">V{{ item.revision_no || 1 }}</span>
                <span>{{ formatTime(item.created_at) }}</span>
                <StatusTag :status="item.status" />
              </button>
            </div>
          </div>

          <!-- 处理中：进度提示 + 轮询 -->
          <div v-if="isProcessing" class="ccc-card processing-card">
            <el-icon class="is-loading processing-icon" :size="28"><Loading /></el-icon>
            <div class="processing-title">{{ processingText }}</div>
            <el-progress
              class="processing-progress"
              :percentage="progressPercent"
              :stroke-width="10"
              :status="progressPercent >= 100 ? 'success' : undefined"
            />
            <div class="processing-sub">{{ processingSubText }}</div>
          </div>

          <!-- 失败态 -->
          <div v-else-if="report.status === 'failed'" class="ccc-card failed-card">
            <el-result icon="error" title="审核处理失败" :sub-title="report.error_msg || '未知错误'">
              <template #extra>
                <el-button
                  v-permission="'reports.review'"
                  type="warning"
                  :loading="reReviewing"
                  @click="onReReview"
                >
                  重新审核
                </el-button>
              </template>
            </el-result>
          </div>

          <template v-else-if="report.status === 'done'">
            <div v-if="trialRecord" class="ccc-card trial-card">
              <div class="card-title trial-title-row">
                <span>8089试运行人工终审</span>
                <el-tag :type="trialNeedsFinal ? 'warning' : 'success'">
                  第 {{ trialRecord.sequence_no }} 份 · {{ trialNeedsFinal ? '待终审' : '已终审' }}
                </el-tag>
              </div>
              <el-alert
                v-if="trialNeedsFinal"
                type="warning"
                :closable="false"
                show-icon
                title="本报告不会自动放行：人工终审完成前，审核意见书下载已锁定。"
              />
              <div v-if="trialNeedsFinal" class="trial-form">
                <div class="trial-audit-overview">
                  <div class="trial-audit-heading">
                    <div>
                      <b>本次终审要核对什么</b>
                      <p>先看系统逐项核对内容和待处理问题，再对照右侧PDF原文；不是只选择一个最终结论。</p>
                    </div>
                    <el-button size="small" type="primary" plain @click="activeTab = 'detail'">查看完整审核明细</el-button>
                  </div>
                  <div class="trial-stat-grid">
                    <div><span>识别样品</span><b>{{ review?.content?.samples?.length || 0 }}</b></div>
                    <div><span>逐项核对</span><b>{{ trialAuditRows.length }}</b></div>
                    <div class="must"><span>必须修改</span><b>{{ mustFixCount }}</b></div>
                    <div class="manual"><span>人工复核</span><b>{{ suggestionCount }}</b></div>
                    <div><span>必审覆盖</span><b>{{ trialCoverageText }}</b></div>
                    <div :class="{ danger: !trialPrivacySafe }"><span>出口残留</span><b>{{ privacyPreflight?.outbound_findings || 0 }}</b></div>
                  </div>
                  <el-collapse class="trial-audit-collapse">
                    <el-collapse-item :title="`系统逐项核对（${trialAuditRows.length}项，通过项也在这里）`" name="checks">
                      <el-table :data="trialAuditRows" stripe max-height="360" size="small">
                        <el-table-column prop="sample" label="样品" min-width="150" show-overflow-tooltip />
                        <el-table-column prop="item" label="核对项目" min-width="130" show-overflow-tooltip />
                        <el-table-column prop="reported" label="报告值" min-width="150" show-overflow-tooltip />
                        <el-table-column prop="required" label="标准要求" min-width="170" show-overflow-tooltip />
                        <el-table-column label="判断" width="92">
                          <template #default="{ row }">
                            <el-tag :type="trialVerdictType(row.verdict)" size="small">{{ trialVerdictText(row.verdict) }}</el-tag>
                          </template>
                        </el-table-column>
                        <el-table-column prop="basis" label="依据" min-width="150" show-overflow-tooltip />
                        <el-table-column label="证据页" width="90">
                          <template #default="{ row }">{{ row.source_pages?.length ? row.source_pages.join('、') : '未定位' }}</template>
                        </el-table-column>
                      </el-table>
                    </el-collapse-item>
                    <el-collapse-item :title="`需要处理或人工复核（${problemItems.length}项）`" name="problems">
                      <el-empty v-if="!problemItems.length" description="系统未提出修改或人工复核项；仍需抽查通过项和PDF原文" :image-size="70" />
                      <el-table v-else :data="problemItems" stripe max-height="320" size="small">
                        <el-table-column prop="item" label="项目" min-width="130" />
                        <el-table-column prop="reported" label="报告情况" min-width="160" show-overflow-tooltip />
                        <el-table-column label="处理要求" min-width="190" show-overflow-tooltip>
                          <template #default="{ row }">{{ row.review_action || row.should_be }}</template>
                        </el-table-column>
                        <el-table-column prop="standard" label="标准依据" min-width="150" show-overflow-tooltip />
                      </el-table>
                    </el-collapse-item>
                  </el-collapse>
                  <el-alert
                    v-if="trialCoverageWarning"
                    type="warning"
                    :closable="false"
                    show-icon
                    :title="trialCoverageWarning"
                  />
                  <div class="trial-review-checklist">
                    <el-checkbox v-model="trialChecklist.problems">已核对系统提出的修改项和人工复核项</el-checkbox>
                    <el-checkbox v-model="trialChecklist.passed">已抽查通过项的报告值、标准要求和判断依据</el-checkbox>
                    <el-checkbox v-model="trialChecklist.source">已对照右侧PDF原文核验关键数据及P/N/F结论</el-checkbox>
                    <el-checkbox v-model="trialChecklist.privacy">已确认内网脱敏及最终出口扫描结果</el-checkbox>
                  </div>
                </div>
                <div class="trial-field">
                  <span class="trial-label">人工结论</span>
                  <el-radio-group v-model="trialFinalForm.final_decision">
                    <el-radio-button value="confirmed">系统结果确认</el-radio-button>
                    <el-radio-button value="corrected">人工更正后确认</el-radio-button>
                    <el-radio-button value="returned">退回修改</el-radio-button>
                  </el-radio-group>
                </div>
                <div class="trial-event-grid">
                  <label><span>发现严重漏审</span><el-switch v-model="trialFinalForm.severe_miss_detected" /></label>
                  <label><span>发生敏感信息外发</span><el-switch v-model="trialFinalForm.sensitive_info_externalized" /></label>
                  <label><span>发生错误自动放行</span><el-switch v-model="trialFinalForm.incorrect_auto_release" /></label>
                </div>
                <el-input
                  v-model="trialFinalForm.review_note"
                  type="textarea"
                  :rows="3"
                  maxlength="2000"
                  show-word-limit
                  placeholder="请记录人工核对范围、更正项或退回原因（必填）"
                />
                <div class="trial-submit-row">
                  <span v-if="trialStatus" class="trial-progress">
                    连续洁净样本 {{ trialStatus.current_clean_streak }}/{{ trialStatus.target }}，仍保留8080回退
                  </span>
                  <el-button
                    v-permission="'reports.review'"
                    type="primary"
                    :loading="trialSubmitting"
                    @click="submitTrialFinal"
                  >提交人工终审</el-button>
                </div>
              </div>
              <el-descriptions v-else :column="2" border>
                <el-descriptions-item label="终审结论">{{ trialDecisionText }}</el-descriptions-item>
                <el-descriptions-item label="终审时间">{{ formatTime(trialRecord.reviewed_at) }}</el-descriptions-item>
                <el-descriptions-item label="三类严重事件" :span="2">
                  {{ trialEventSummary }}
                </el-descriptions-item>
                <el-descriptions-item label="终审记录" :span="2">{{ trialRecord.review_note || '-' }}</el-descriptions-item>
              </el-descriptions>
            </div>

            <!-- 审核结论卡片 -->
            <div class="ccc-card conclusion-card">
              <div class="card-title">审核结论</div>
              <div class="conclusion-body">
                <el-tag
                  :type="conclusionTagType"
                  size="large"
                  effect="dark"
                  class="conclusion-tag"
                >
                  {{ report.conclusion || review?.conclusion || '-' }}
                </el-tag>
                <div v-if="mustFixCount || suggestionCount" class="conclusion-counts">
                  <span v-if="mustFixCount" class="count-item must">
                    必须修改 <b>{{ mustFixCount }}</b> 项
                  </span>
                  <span v-if="suggestionCount" class="count-item suggestion">
                    人工复核 <b>{{ suggestionCount }}</b> 项
                  </span>
                </div>
              </div>
            </div>

            <div v-if="privacyPreflight" class="ccc-card">
              <div class="card-title">双阶段外网审核与外发安全</div>
              <el-descriptions :column="3" border>
                <el-descriptions-item label="运行模式">
                  <el-tag :type="privacyPreflight.mode === 'enforce' ? 'success' : 'warning'">{{ privacyPreflight.mode === 'enforce' ? '正式脱敏' : '影子观察' }}</el-tag>
                </el-descriptions-item>
                <el-descriptions-item label="企业名称替换">{{ privacyPreflight.company_replacements || 0 }} 处</el-descriptions-item>
                <el-descriptions-item label="申请编号替换">{{ privacyPreflight.application_replacements || 0 }} 处</el-descriptions-item>
                <el-descriptions-item label="处理批次">{{ externalContentReview?.batch_count || privacyPreflight.batch_count || 0 }}</el-descriptions-item>
                <el-descriptions-item label="第一阶段内容复核">{{ externalContentReview ? `${externalContentReview.successful_batches || 0}/${externalContentReview.batch_count || 0}` : (privacyPreflight.semantic_model_used ? '旧版内网复核' : '未调用') }}</el-descriptions-item>
                <el-descriptions-item label="出口残留">{{ privacyPreflight.outbound_findings || 0 }}</el-descriptions-item>
              </el-descriptions>
              <el-alert v-if="privacyPreflight.mode === 'shadow'" class="mt-12" type="warning" :closable="false" show-icon title="当前为影子观察：已生成脱敏副本和检查记录，但尚未改变主审核模型收到的内容。" />
              <el-alert v-if="externalContentReview?.failed_batches" class="mt-12" type="warning" :closable="false" show-icon :title="`第一阶段外网内容复核有 ${externalContentReview.failed_batches}/${externalContentReview.batch_count} 批失败；第二阶段主审核已继续，失败不会被当作报告问题。`" />
              <el-alert v-else-if="privacyPreflight.model_errors" class="mt-12" type="warning" :closable="false" show-icon title="该历史结果存在旧版内网复核失败记录；重新审核后将使用双阶段外网流程。" />
              <div v-if="privacyLogicRows.length" class="privacy-merge-summary">
                第一阶段内容复核记录 {{ contentLogicChecks.length }} 条，按同类内容合并为 {{ privacyLogicRows.length }} 组；线索已交由第二阶段回到原文和规则库核实，不直接等于最终不符合。
              </div>
              <el-table v-if="privacyLogicRows.length" :data="privacyLogicRows" row-key="key" stripe class="mt-12">
                <el-table-column type="expand" width="44">
                  <template #default="{ row }">
                    <div class="privacy-detail-list">
                      <div v-for="(detail, index) in row.details" :key="`${row.key}-${index}`" class="privacy-detail-item">
                        <span class="privacy-detail-index">{{ index + 1 }}</span>
                        <div><b>{{ detail.item }}</b><p>{{ detail.note || '未提供说明' }}</p></div>
                      </div>
                    </div>
                  </template>
                </el-table-column>
                <el-table-column prop="item" label="逻辑检查" min-width="120" />
                <el-table-column label="复核状态" width="110">
                  <template #default="{ row }"><el-tag :type="row.result === 'pass' ? 'success' : 'warning'">{{ row.result === 'pass' ? '未见矛盾' : row.result === 'warning' ? '复核提示' : '待核实线索' }}</el-tag></template>
                </el-table-column>
                <el-table-column label="次数" width="60" align="center">
                  <template #default="{ row }"><el-tag effect="plain" type="info">{{ row.count }}</el-tag></template>
                </el-table-column>
                <el-table-column prop="summary" label="合并说明" min-width="140" show-overflow-tooltip />
              </el-table>
            </div>

            <!-- 审核意见书 -->
            <div v-loading="reviewLoading" class="ccc-card review-card">
              <el-tabs v-model="activeTab">
                <el-tab-pane v-if="revisionComparison" label="更正验证" name="revision">
                  <div class="revision-summary">
                    <el-tag :type="revisionStatusType" size="large" effect="dark">{{ revisionComparison.status }}</el-tag>
                    <span>原问题 {{ revisionComparison.original_issue_count }} 项</span>
                    <span class="resolved-text">已解决 {{ revisionComparison.resolved_count }} 项</span>
                    <span class="unresolved-text">未解决 {{ revisionComparison.unresolved_count }} 项</span>
                    <span>待复核 {{ revisionComparison.manual_review_count }} 项</span>
                    <span>新增问题 {{ revisionComparison.new_issue_count }} 项</span>
                  </div>
                  <el-table :data="revisionComparison.items" stripe>
                    <el-table-column prop="sample" label="样品" min-width="130" show-overflow-tooltip />
                    <el-table-column prop="original_item" label="原问题" min-width="130" show-overflow-tooltip />
                    <el-table-column prop="original_reported" label="原报告值" min-width="110" show-overflow-tooltip />
                    <el-table-column prop="required_correction" label="更正要求" min-width="150" show-overflow-tooltip />
                    <el-table-column prop="new_evidence" label="新版证据" min-width="180" show-overflow-tooltip />
                    <el-table-column label="验证状态" width="110" align="center">
                      <template #default="{ row }">
                        <el-tag :type="revisionItemType(row.status)" size="small">{{ revisionItemText(row.status) }}</el-tag>
                      </template>
                    </el-table-column>
                  </el-table>
                </el-tab-pane>
                <!-- 问题与人工复核项 -->
                <el-tab-pane :label="`问题与复核(${problemItems.length})`" name="problems">
                  <div class="tab-tip">
                    有证据页的项目可点击页码查看原文；页码为 PDF 文件页序。未定位的项目仍需对照原文核查。
                  </div>
                  <el-table :data="problemItems" stripe>
                    <el-table-column label="样品" min-width="140">
                      <template #default="{ row }">
                        <div class="sample-model">{{ row.model }}</div>
                        <div class="sample-spec">{{ row.spec }}</div>
                      </template>
                    </el-table-column>
                    <el-table-column prop="item" label="检测项目" min-width="120" show-overflow-tooltip />
                    <el-table-column prop="reported" label="报告原值" min-width="110" show-overflow-tooltip />
                    <el-table-column label="处理要求" min-width="140" show-overflow-tooltip>
                      <template #default="{ row }">
                        <span class="should-be">{{ row.review_action || row.should_be }}</span>
                      </template>
                    </el-table-column>
                    <el-table-column prop="standard" label="标准依据" min-width="160" show-overflow-tooltip />
                    <el-table-column label="证据页" min-width="100">
                      <template #default="{ row }">
                        <template v-if="evidencePages(row).length">
                          <el-button v-for="page in evidencePages(row)" :key="page" link type="primary" :disabled="!pdfUrl || pdfPageCount === null || page > pdfPageCount" @click="showEvidencePage(page)">第{{ page }}页{{ pdfPageCount !== null && page > pdfPageCount ? '（超出文件页数）' : '' }}</el-button>
                          <small v-if="pdfUrl && pdfPageCount === null">页数未确认，请手动查看原文</small>
                        </template>
                        <span v-else>未定位</span>
                      </template>
                    </el-table-column>
                    <el-table-column label="严重级别" width="90" align="center">
                      <template #default="{ row }">
                        <el-tag v-if="row.severity === 'must_fix'" type="danger" size="small">必须改</el-tag>
                        <el-tag v-else type="warning" size="small">待复核</el-tag>
                      </template>
                    </el-table-column>
                    <el-table-column label="操作" width="78" fixed="right" align="center">
                      <template #default="{ row }">
                        <el-button v-permission="'reports.review'" link type="primary" @click="openCorrection(row)">
                          纠错
                        </el-button>
                      </template>
                    </el-table-column>
                    <template #empty>
                      <el-empty description="无需修改项，报告审核通过" :image-size="80" />
                    </template>
                  </el-table>
                </el-tab-pane>

                <!-- 备注建议 -->
                <el-tab-pane :label="`备注建议(${remarks.length})`" name="remarks">
                  <el-empty v-if="!remarks.length" description="无备注建议" :image-size="80" />
                  <div v-else class="remark-list">
                    <div v-for="(remark, i) in remarks" :key="i" class="remark-item">
                      <span class="remark-no">{{ i + 1 }}</span>
                      <span class="remark-text">{{ remark }}</span>
                    </div>
                  </div>
                </el-tab-pane>

                <!-- 审核明细（Markdown 渲染） -->
                <el-tab-pane label="审核明细" name="detail">
                  <div class="tab-tip">按样品展示系统实际核对的报告值、标准要求和判断依据；未提取或不确定的内容会明确标为待复核。</div>
                  <el-empty v-if="!detailHtml" description="暂无审核明细" :image-size="80" />
                  <!-- 后端生成的可信内容，轻量渲染标题/表格/列表/粗体 -->
                  <div v-else class="md-preview" v-html="detailHtml"></div>
                </el-tab-pane>

                <el-tab-pane :label="`纠错记录(${corrections.length})`" name="corrections">
                  <div class="tab-tip">纠错提交后由管理员确认；只有加入规则版本并发布后，才会影响后续审核。</div>
                  <el-table v-loading="correctionsLoading" :data="corrections" stripe>
                    <el-table-column prop="correction_type_text" label="纠错类型" width="130" />
                    <el-table-column prop="description" label="纠错说明" min-width="220" show-overflow-tooltip />
                    <el-table-column prop="standard_ref" label="标准依据" min-width="160" show-overflow-tooltip />
                    <el-table-column label="状态" width="90" align="center">
                      <template #default="{ row }">
                        <el-tag :type="correctionStatusType(row.status)" size="small">
                          {{ correctionStatusText(row.status) }}
                        </el-tag>
                      </template>
                    </el-table-column>
                    <el-table-column prop="submitter_name" label="提交人" width="100" />
                    <el-table-column label="提交时间" width="160">
                      <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
                    </el-table-column>
                    <template #empty>
                      <el-empty description="这份报告还没有纠错记录" :image-size="80" />
                    </template>
                  </el-table>
                </el-tab-pane>
              </el-tabs>
            </div>
          </template>
        </el-col>

        <!-- 右侧 PDF 预览面板 -->
        <el-col v-show="!pdfCollapsed" :xs="24" :lg="11">
          <div class="ccc-card pdf-card">
            <div class="pdf-header">
              <span class="card-title">报告原文</span>
              <el-button
                text
                type="primary"
                size="small"
                :disabled="!pdfUrl"
                @click="openPdfNewWindow"
              >
                在新窗口打开
              </el-button>
            </div>
            <div v-if="pdfLoading" class="pdf-body pdf-center">
              <el-icon class="is-loading" :size="26"><Loading /></el-icon>
              <div class="pdf-tip">PDF 加载中…</div>
            </div>
            <div v-else-if="pdfError" class="pdf-body pdf-center">
              <el-empty description="原文 PDF 加载失败" :image-size="80" />
            </div>
            <iframe v-else-if="pdfUrl" :key="pdfViewerRevision" :src="pdfViewerUrl" class="pdf-body pdf-frame" title="报告原文PDF"></iframe>
          </div>
        </el-col>
      </el-row>

      <el-dialog v-model="correctionDialogVisible" title="提交审核纠错" width="min(680px, 92vw)" destroy-on-close>
        <el-alert
          title="请写清正确做法和适用范围。提交后不会立即改变审核规则，需要管理员确认并发布版本。"
          type="info"
          :closable="false"
          show-icon
          class="correction-alert"
        />
        <el-form ref="correctionFormRef" :model="correctionForm" :rules="correctionRules" label-position="top">
          <el-form-item label="纠错类型" prop="correction_type">
            <el-select v-model="correctionForm.correction_type" class="full-width">
              <el-option label="误报（系统指出的问题其实不存在）" value="false_positive" />
              <el-option label="漏报（报告有问题但系统未指出）" value="missed_issue" />
              <el-option label="标准依据错误" value="wrong_basis" />
              <el-option label="数值或计算错误" value="wrong_value" />
              <el-option label="基础信息识别错误" value="wrong_metadata" />
              <el-option label="审核结论错误" value="conclusion_error" />
            </el-select>
          </el-form-item>
          <div v-if="selectedOriginalItem" class="original-item-box">
            <div><span>原检测项目</span>{{ selectedOriginalItem.item || '-' }}</div>
            <div><span>原报告值</span>{{ selectedOriginalItem.reported || '-' }}</div>
            <div><span>原应改值</span>{{ selectedOriginalItem.should_be || '-' }}</div>
            <div><span>原标准依据</span>{{ selectedOriginalItem.standard || '-' }}</div>
          </div>
          <el-form-item label="纠错说明" prop="description">
            <el-input
              v-model="correctionForm.description"
              type="textarea"
              :rows="3"
              maxlength="4000"
              show-word-limit
              placeholder="例如：该型号不适用此条款，这一项属于误报；正确判断应为……"
            />
          </el-form-item>
          <el-form-item label="正确结果或处理方式" prop="corrected_text">
            <el-input
              v-model="correctionForm.corrected_text"
              type="textarea"
              :rows="3"
              placeholder="写明正确数值、正确结论或系统今后应如何判断"
            />
          </el-form-item>
          <el-form-item label="标准依据（建议填写）">
            <el-input v-model="correctionForm.standard_ref" placeholder="例如：GB/T 2951.31-2008 第 8.2 条" />
          </el-form-item>
          <el-form-item label="适用产品单元或条件（建议填写）">
            <el-input
              v-model="correctionForm.applicable_conditions"
              type="textarea"
              :rows="2"
              placeholder="例如：仅适用于 RVV，且报告中明确给出外径和绝缘厚度时"
            />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="correctionDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="correctionSubmitting" @click="submitCorrection">提交给管理员</el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="revisionDialogVisible" title="上传实验室更正版本" width="min(620px, 92vw)" destroy-on-close>
        <el-alert
          :title="`新文件将作为 V${(report.revision_no || 1) + 1} 保存，V${report.revision_no || 1} 的 PDF 和审核记录会完整保留。系统会复核原问题，并检查是否出现新问题。`"
          type="info"
          :closable="false"
          show-icon
          class="correction-alert"
        />
        <el-form label-position="top">
          <el-form-item label="更正后的 PDF">
            <el-upload
              drag
              action="#"
              accept=".pdf,application/pdf"
              :auto-upload="false"
              :limit="1"
              :on-change="onRevisionFileChange"
              :on-remove="onRevisionFileRemove"
            >
              <div class="upload-copy">点击或拖入更正后的 PDF</div>
              <template #tip><div class="el-upload__tip">不会覆盖当前版本</div></template>
            </el-upload>
          </el-form-item>
          <el-form-item label="更正说明">
            <el-input
              v-model="revisionNote"
              type="textarea"
              :rows="3"
              maxlength="1000"
              show-word-limit
              placeholder="例如：已更正样品2的绝缘变化率和低温试验判定"
            />
          </el-form-item>
          <el-progress v-if="revisionUploading" :percentage="revisionUploadPercent" />
          <el-alert
            v-if="revisionCategoryConflict"
            type="warning"
            :closable="false"
            show-icon
            class="correction-alert"
            :title="`类别不一致：当前是${revisionCategoryConflict.original_category_label || '原类别'}，新文件识别为${revisionCategoryConflict.detected_category_label || '另一类别'}。不能加入当前版本链。`"
            description="可以沿用已核验的申请编号和试验任务编号，将新文件建立为独立类别报告。"
          />
        </el-form>
        <template #footer>
          <el-button :disabled="revisionUploading" @click="revisionDialogVisible = false">取消</el-button>
          <el-button
            v-if="revisionCategoryConflict"
            type="success"
            :loading="revisionUploading"
            @click="submitIndependentRevisionCategory"
          >
            作为{{ revisionCategoryConflict.detected_category_label || '独立类别' }}上传
          </el-button>
          <el-button v-else type="primary" :loading="revisionUploading" @click="submitRevision">上传并开始复审</el-button>
        </template>
      </el-dialog>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules, TagProps, UploadFile } from 'element-plus'
import { Back, Download, RefreshRight, Loading, View, Hide } from '@element-plus/icons-vue'
import { marked } from 'marked'
import { formatSystemTime } from '@/utils/datetime'
import { normalizeNumericRanges } from '@/utils/markdown'
import { useAppStore } from '@/stores/app'
import StatusTag from '@/components/StatusTag.vue'
import {
  downloadReview,
  getReportDetail,
  getReportPdfDocument,
  getReportReview,
  getReportStatus,
  getReportVersions,
  getTrialRecord,
  getTrialStatus,
  finalizeTrialReport,
  reReview,
  uploadCorrectionVersion,
  uploadIndependentCategory
} from '@/api/reports'
import type { ReportDetailData, ReportItem, ReviewItem, ReviewOut, UploadConflictDetail } from '@/types'
import type { ReportStatusOut, TrialRecordOut, TrialStatusOut } from '@/api/reports'
import { createCorrection, getReportCorrections } from '@/api/iteration'
import type { CorrectionRecord } from '@/api/iteration'

const route = useRoute()
const router = useRouter()
const app = useAppStore()
const reportId = route.params.id as string

// ===== 页面状态 =====
const report = ref<ReportDetailData | null>(null)
const review = ref<ReviewOut | null>(null)
const pageLoading = ref(true)
const reviewLoading = ref(false)
const loadError = ref('')
const reReviewing = ref(false)
const activeTab = ref('problems')
const liveStatus = ref<ReportStatusOut | null>(null)
const corrections = ref<CorrectionRecord[]>([])
const correctionsLoading = ref(false)
const correctionDialogVisible = ref(false)
const correctionSubmitting = ref(false)
const correctionFormRef = ref<FormInstance>()
const selectedOriginalItem = ref<(ReviewItem & { model?: string; spec?: string }) | null>(null)
const correctionForm = ref({
  correction_type: 'false_positive',
  description: '',
  corrected_text: '',
  standard_ref: '',
  applicable_conditions: ''
})
const correctionRules: FormRules = {
  correction_type: [{ required: true, message: '请选择纠错类型', trigger: 'change' }],
  description: [{ required: true, min: 2, message: '请说明具体错在哪里', trigger: 'blur' }],
  corrected_text: [{ required: true, min: 2, message: '请填写正确结果或处理方式', trigger: 'blur' }]
}
const versions = ref<ReportItem[]>([])
const revisionDialogVisible = ref(false)
const revisionFile = ref<File | null>(null)
const revisionNote = ref('')
const revisionUploading = ref(false)
const revisionUploadPercent = ref(0)
const revisionCategoryConflict = ref<UploadConflictDetail | null>(null)
const trialStatus = ref<TrialStatusOut | null>(null)
const trialRecord = ref<TrialRecordOut | null>(null)
const trialSubmitting = ref(false)
const trialFinalForm = ref({
  final_decision: 'confirmed' as 'confirmed' | 'corrected' | 'returned',
  severe_miss_detected: false,
  sensitive_info_externalized: false,
  incorrect_auto_release: false,
  review_note: ''
})
const trialChecklist = ref({ problems: false, passed: false, source: false, privacy: false })

// ===== PDF 预览 =====
const pdfUrl = ref('')
const pdfLoading = ref(false)
const pdfError = ref(false)
const pdfCollapsed = ref(false)
const pdfPageCount = ref<number | null>(null)
const pdfEvidencePage = ref<number | null>(null)
const pdfViewerRevision = ref(0)
const pdfViewerUrl = computed(() => pdfEvidencePage.value === null
  ? pdfUrl.value : `${pdfUrl.value.split('#')[0]}#page=${pdfEvidencePage.value}`)
function evidencePages(row: { source_pages?: unknown }): number[] {
  if (!Array.isArray(row.source_pages)) return []
  return [...new Set(row.source_pages.filter((page): page is number =>
    typeof page === 'number' && Number.isSafeInteger(page) && page > 0))].sort((a, b) => a - b)
}
function showEvidencePage(page: number) {
  if (!pdfUrl.value || pdfPageCount.value === null || !Number.isSafeInteger(page) || page < 1 || page > pdfPageCount.value) return
  pdfCollapsed.value = false
  pdfEvidencePage.value = page
  // Recreate the native PDF viewer: changing only its fragment can be ignored.
  pdfViewerRevision.value += 1
}

/** 轮询定时器 */
let pollTimer: ReturnType<typeof setInterval> | null = null

/** 时间格式化 */
function formatTime(time?: string | null): string {
  return formatSystemTime(time, app.systemConfig.timezone)
}

/** 是否处理中（需要轮询） */
const isProcessing = computed(() =>
  ['pending', 'extracting', 'reviewing'].includes(report.value?.status || '')
)

/** 处理中阶段文案 */
const processingText = computed(() => {
  if (liveStatus.value?.progress_message) return liveStatus.value.progress_message
  const map: Record<string, string> = {
    pending: '已排队，等待系统处理…',
    extracting: '正在提取报告文本…',
    reviewing: 'AI 审核中，请稍候…'
  }
  return map[report.value?.status || ''] || '处理中…'
})

const progressPercent = computed(() => liveStatus.value?.progress_percent || 0)

const processingSubText = computed(() => {
  const status = liveStatus.value
  const pieces: string[] = []
  if (status?.progress_total) pieces.push(`${status.progress_current}/${status.progress_total}`)
  if (status?.attempt) pieces.push(`第 ${status.attempt} 次执行`)
  return pieces.length
    ? `${pieces.join(' · ')}，完成后将自动展示结果`
    : '系统正在自动处理，完成后将自动展示结果'
})

/** 结论大标签颜色：合格绿 / 需修改橙红 */
const conclusionTagType = computed<'success' | 'warning' | 'danger' | 'info'>(() => {
  const text = report.value?.conclusion || review.value?.conclusion || ''
  if (text.includes('合格')) return 'success'
  const match = text.match(/需修改\s*(\d+)\s*处/)
  if (match) return Number(match[1]) > 5 ? 'danger' : 'warning'
  if (text.includes('需修改')) return 'warning'
  return 'info'
})

/** 拍平所有样品的审核项 */
const allItems = computed<(ReviewItem & { model: string; spec: string })[]>(() => {
  const samples = review.value?.content?.samples || []
  const reportItems = (review.value?.content?.report_items || []).map((it) =>
    ({ ...it, model: '全报告', spec: '报告一致性' }))
  return reportItems.concat(samples.flatMap((s) =>
    (s.items || []).map((it) => ({ ...it, model: s.model, spec: s.spec }))
  ))
})

/** 只展示真正需要修改或人工复核的项目，正确性确认不进入问题列表。 */
const problemItems = computed(() => allItems.value.filter(
  (it) => it.severity !== 'ok' && it.action_required !== false
))

const mustFixCount = computed(() => problemItems.value.filter((it) => it.severity === 'must_fix').length)
const suggestionCount = computed(() => problemItems.value.filter((it) => it.severity === 'suggestion').length)
const revisionComparison = computed(() => review.value?.content?.revision_comparison || null)
const privacyPreflight = computed(() => review.value?.content?._review_meta?.privacy_preflight || null)
const externalContentReview = computed(() => review.value?.content?._review_meta?.external_content_review || null)
const contentLogicChecks = computed(() => externalContentReview.value?.logic_checks || privacyPreflight.value?.logic_checks || [])
type PrivacyLogicDetail = { item: string; result: string; note: string }
type PrivacyLogicRow = {
  key: string
  item: string
  result: string
  count: number
  summary: string
  details: PrivacyLogicDetail[]
}

function canonicalPrivacyItem(value: string): string {
  const text = value.replace(/[\s/]/g, '')
  if (['样品数量与子报告数量', '样品数量与报告组成一致性', '报告组成与样品数量对应', '样品数量与报告组成对应', '报告组成子编号与样品数量对应'].includes(text)) return '样品数量与报告组成对应'
  if (['申请类型与备注内容一致性', '申请类型与备注一致性', '报告类型勾选与备注一致性'].includes(text)) return '申请类型与备注一致性'
  if (text.includes('型号') && text.includes('规格') && text.includes('电压') && text.includes('一致')) return '型号、规格与电压等级一致性'
  if (text.includes('报告编号') && text.includes('一致')) return '报告编号一致性'
  if (text.includes('申请编号') && text.includes('一致')) return '申请编号一致性'
  if (text.includes('型号') && text.includes('规格') && text.includes('一致')) return '型号与规格一致性'
  if (text.includes('型号') && text.includes('电压') && text.includes('一致')) return '型号与电压等级一致性'
  if (text.includes('电压等级') && text.includes('一致')) return '电压等级一致性'
  if ((text.includes('执行标准') || text.includes('依据标准')) && text.includes('一致')) return '执行标准一致性'
  if (text === 'PN判定与试验结论' || /P.*N.*(?:F)?.*结论.*一致|(?:P|N|F)判定.*结论.*一致/i.test(text)) return 'P/N/F判定与试验结论一致性'
  if (text.includes('委托人') && text.includes('生产') && text.includes('一致')) return '委托人/生产者/生产企业一致性'
  return value.trim() || '未命名检查'
}

const privacyLogicRows = computed<PrivacyLogicRow[]>(() => {
  const checks = contentLogicChecks.value
  const groups = new Map<string, PrivacyLogicRow>()
  for (const raw of checks) {
    const detail: PrivacyLogicDetail = {
      item: String(raw.item || '').trim(),
      result: String(raw.result || 'manual_review').trim(),
      note: String(raw.note || '').trim()
    }
    const item = canonicalPrivacyItem(detail.item)
    const key = `${item}\u0000${detail.result}`
    const row = groups.get(key) || { key, item, result: detail.result, count: 0, summary: '', details: [] }
    if (!row.details.some((existing) => existing.item === detail.item && existing.note === detail.note)) {
      row.details.push(detail)
    }
    row.count += 1
    groups.set(key, row)
  }
  return [...groups.values()].map((row) => {
    const notes = [...new Set(row.details.map((detail) => detail.note).filter(Boolean))]
    row.summary = row.count === 1
      ? (notes[0] || '未提供说明')
      : row.result === 'pass'
        ? `已完成 ${row.count} 处同类核对，均未发现矛盾`
        : notes.length === 1
          ? notes[0]
          : `${row.count} 处检查发现同类疑点，请展开查看具体说明`
    return row
  }).sort((left, right) => {
    const priority = (result: string) => result === 'pass' ? 2 : result === 'warning' ? 0 : 1
    return priority(left.result) - priority(right.result) || left.item.localeCompare(right.item, 'zh-CN')
  })
})
const isLatestVersion = computed(() => {
  if (!report.value || !versions.value.length) return true
  return report.value.id === versions.value[versions.value.length - 1]?.id
})
const trialNeedsFinal = computed(() => trialRecord.value?.human_status === 'pending')
const trialAuditRows = computed(() => [
  ...(review.value?.content?.report_checks || []).map((check) => ({ ...check, sample: '全报告' })),
  ...(review.value?.content?.samples || []).flatMap((sample) =>
  (sample.checks || []).map((check) => ({
    ...check,
    sample: [sample.model, sample.voltage, sample.spec].filter(Boolean).join(' ')
  }))
)])
const trialCoverage = computed(() => review.value?.content?._deterministic_validation?.required_item_coverage || null)
const trialCoverageText = computed(() => {
  const coverage = trialCoverage.value
  if (!coverage) return '未生成'
  return `${coverage.required_covered || 0}/${coverage.required_total || 0}`
})
const trialCoverageWarning = computed(() => {
  const coverage = trialCoverage.value
  if (!coverage) return '本报告未生成必审覆盖统计，终审时必须人工确认是否审全。'
  const missing = coverage.missing?.length || 0
  const unmatched = coverage.unmatched_samples?.length || 0
  const conditionalUnknown = coverage.conditional_unknown || 0
  const messages = [
    missing && `仍有${missing}个必审项目缺少确定性覆盖`,
    unmatched && `有${unmatched}个样品未匹配结构化型号矩阵`,
    conditionalUnknown && `有${conditionalUnknown}个条件项目适用性未确定`
  ].filter(Boolean)
  return messages.length ? `${messages.join('；')}，不得直接确认放行。` : ''
})
const trialPrivacySafe = computed(() =>
  Boolean(privacyPreflight.value)
  && Number(privacyPreflight.value?.outbound_findings || 0) === 0
  && Number(externalContentReview.value?.failed_batches || 0) === 0
)
function trialVerdictText(value: string): string {
  return ({ pass: '通过', fail: '不符合', not_applicable: '不适用', manual_review: '人工复核' } as Record<string, string>)[value] || value || '-'
}
function trialVerdictType(value: string): TagProps['type'] {
  if (value === 'pass') return 'success'
  if (value === 'fail') return 'danger'
  if (value === 'manual_review') return 'warning'
  return 'info'
}
const trialDecisionText = computed(() => ({
  confirmed: '系统结果确认', corrected: '人工更正后确认', returned: '退回修改'
} as Record<string, string>)[trialRecord.value?.final_decision || ''] || '-')
const trialEventSummary = computed(() => {
  if (!trialRecord.value) return '-'
  const events = [
    trialRecord.value.severe_miss_detected && '严重漏审',
    trialRecord.value.sensitive_info_externalized && '敏感信息外发',
    trialRecord.value.incorrect_auto_release && '错误自动放行'
  ].filter(Boolean)
  return events.length ? events.join('、') : '未发现'
})
const revisionStatusType = computed<TagProps['type']>(() => {
  const status = revisionComparison.value?.status || ''
  if (status === '更正通过') return 'success'
  if (status.includes('新问题') || status === '部分更正') return 'danger'
  return 'warning'
})

/** 备注建议列表 */
const remarks = computed(() => review.value?.content?.remarks || [])

/** 审核明细 Markdown → HTML */
const detailHtml = computed(() => {
  const detail = review.value?.content?.detail
  return detail ? (marked.parse(normalizeNumericRanges(detail)) as string) : ''
})

/** 加载报告详情 */
async function loadDetail() {
  report.value = await getReportDetail<ReportDetailData>(reportId)
}

async function loadVersions() {
  versions.value = await getReportVersions(reportId)
}

/** 加载审核意见书 */
async function loadReview() {
  reviewLoading.value = true
  try {
    review.value = await getReportReview(reportId)
  } catch {
    // 错误提示由拦截器统一弹出
  } finally {
    reviewLoading.value = false
  }
}

async function loadTrial() {
  const status = await getTrialStatus()
  trialStatus.value = status
  if (
    status?.mode === '8089_formal_trial'
    && Number(reportId) > status.start_after_report_id
  ) {
    trialRecord.value = await getTrialRecord(reportId)
  } else {
    trialRecord.value = null
  }
}

async function submitTrialFinal() {
  if (!Object.values(trialChecklist.value).every(Boolean)) {
    ElMessage.warning('请先完成四项终审核对并逐项勾选')
    return
  }
  if (!trialPrivacySafe.value && !trialFinalForm.value.sensitive_info_externalized) {
    ElMessage.warning('内容复核或外发安全检查存在异常，请核对后标记敏感信息外发事件或处理异常')
    return
  }
  if (trialFinalForm.value.review_note.trim().length < 2) {
    ElMessage.warning('请填写人工终审记录')
    return
  }
  trialSubmitting.value = true
  try {
    trialRecord.value = await finalizeTrialReport(reportId, {
      ...trialFinalForm.value,
      review_note: trialFinalForm.value.review_note.trim()
    })
    trialStatus.value = await getTrialStatus()
    ElMessage.success('人工终审已记录，本报告已解锁下载')
  } finally {
    trialSubmitting.value = false
  }
}

async function loadCorrections() {
  correctionsLoading.value = true
  try {
    corrections.value = await getReportCorrections(reportId)
  } catch {
    // 不影响报告主体展示
  } finally {
    correctionsLoading.value = false
  }
}

function openCorrection(item?: ReviewItem & { model?: string; spec?: string }) {
  selectedOriginalItem.value = item || null
  correctionForm.value = {
    correction_type: item ? 'false_positive' : 'missed_issue',
    description: '',
    corrected_text: '',
    standard_ref: item?.standard || '',
    applicable_conditions: report.value?.product_unit ? `产品单元：${report.value.product_unit}` : ''
  }
  correctionDialogVisible.value = true
}

async function submitCorrection() {
  if (!correctionFormRef.value || !(await correctionFormRef.value.validate().catch(() => false))) return
  correctionSubmitting.value = true
  try {
    const original = selectedOriginalItem.value
      ? { ...selectedOriginalItem.value }
      : {
          application_no: report.value?.application_no || '',
          company_name: report.value?.company?.name || '',
          product_unit: report.value?.product_unit || '',
          conclusion: report.value?.conclusion || review.value?.conclusion || ''
        }
    await createCorrection({
      report_id: Number(reportId),
      correction_type: correctionForm.value.correction_type,
      original_json: original,
      corrected_json: {
        correction: correctionForm.value.corrected_text,
        applicable_conditions: correctionForm.value.applicable_conditions
      },
      description: correctionForm.value.description,
      standard_ref: correctionForm.value.standard_ref
    })
    ElMessage.success('纠错已提交，等待管理员确认')
    correctionDialogVisible.value = false
    await loadCorrections()
    activeTab.value = 'corrections'
  } finally {
    correctionSubmitting.value = false
  }
}

function correctionStatusText(status: string) {
  return ({ pending: '待确认', approved: '已通过', rejected: '已驳回' } as Record<string, string>)[status] || status
}

function correctionStatusType(status: string): TagProps['type'] {
  return status === 'approved' ? 'success' : status === 'rejected' ? 'danger' : 'warning'
}

function revisionItemText(status: string) {
  return ({ resolved: '已解决', unresolved: '未解决', manual_review: '待人工复核' } as Record<string, string>)[status] || status
}

function revisionItemType(status: string): TagProps['type'] {
  return status === 'resolved' ? 'success' : status === 'unresolved' ? 'danger' : 'warning'
}

function goVersion(id: number) {
  if (id !== report.value?.id) window.location.assign(`/reports/${id}`)
}

function openRevisionDialog() {
  revisionFile.value = null
  revisionNote.value = ''
  revisionUploadPercent.value = 0
  revisionCategoryConflict.value = null
  revisionDialogVisible.value = true
}

function onRevisionFileChange(uploadFile: UploadFile) {
  revisionFile.value = uploadFile.raw || null
  revisionCategoryConflict.value = null
}

function onRevisionFileRemove() {
  revisionFile.value = null
  revisionCategoryConflict.value = null
}

function revisionUploadConflict(error: unknown): UploadConflictDetail | null {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (!detail || typeof detail !== 'object' || !(detail as UploadConflictDetail).code) return null
  return detail as UploadConflictDetail
}

async function submitRevision() {
  if (!revisionFile.value) {
    ElMessage.warning('请先选择更正后的 PDF')
    return
  }
  revisionUploading.value = true
  revisionUploadPercent.value = 0
  try {
    const created = await uploadCorrectionVersion(
      reportId,
      revisionFile.value,
      revisionNote.value,
      (percent) => { revisionUploadPercent.value = percent }
    )
    ElMessage.success(`V${created.revision_no} 已上传，正在开始复审`)
    revisionDialogVisible.value = false
    window.location.assign(`/reports/${created.id}`)
  } catch (error) {
    const conflict = revisionUploadConflict(error)
    if (conflict?.code === 'correction_category_mismatch') {
      revisionCategoryConflict.value = conflict
      ElMessage.warning('新文件属于另一报告类别，请确认是否作为独立类别上传')
    } else {
      throw error
    }
  } finally {
    revisionUploading.value = false
  }
}

async function submitIndependentRevisionCategory() {
  if (!revisionFile.value || !revisionCategoryConflict.value?.report_id) return
  revisionUploading.value = true
  revisionUploadPercent.value = 0
  try {
    const conflict = revisionCategoryConflict.value
    const created = await uploadIndependentCategory(
      conflict.report_id,
      revisionFile.value,
      (percent) => { revisionUploadPercent.value = percent }
    )
    ElMessage.success(`已作为${conflict.detected_category_label || '独立类别'} V${created.revision_no || 1} 上传`)
    revisionDialogVisible.value = false
    window.location.assign(`/reports/${created.id}`)
  } finally {
    revisionUploading.value = false
  }
}

/** 加载原文 PDF（blob → iframe 预览） */
async function loadPdf() {
  pdfLoading.value = true
  pdfError.value = false
  try {
    const document = await getReportPdfDocument(reportId)
    pdfUrl.value = document.url
    pdfPageCount.value = document.pageCount
  } catch {
    pdfError.value = true
  } finally {
    pdfLoading.value = false
  }
}

/** 停止轮询 */
function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function refreshStatus() {
    try {
      const s = await getReportStatus(reportId)
      if (!report.value) return
      liveStatus.value = s
      report.value.status = s.status
      report.value.error_msg = s.error_msg
      if (s.application_no) report.value.application_no = s.application_no
      if (s.report_no) report.value.report_no = s.report_no
      if (s.product_unit) report.value.product_unit = s.product_unit
      if (s.company_name) {
        report.value.company = report.value.company || { id: 0, name: s.company_name }
        report.value.company.name = s.company_name
      }
      if (s.conclusion) report.value.conclusion = s.conclusion
      if (s.status === 'done') {
        stopPolling()
        ElMessage.success('审核完成')
        await loadReview()
        await loadTrial()
      } else if (s.status === 'failed') {
        stopPolling()
      }
    } catch {
      // 单次轮询失败忽略，等待下一轮
    }
}

/** 启动状态轮询（每 3 秒），完成后自动加载审核结果 */
function startPolling() {
  stopPolling()
  refreshStatus()
  pollTimer = setInterval(refreshStatus, 3000)
}

/** 下载审核意见书 Markdown */
async function onDownload() {
  try {
    await downloadReview(reportId, report.value?.application_no || report.value?.report_no)
    ElMessage.success('审核意见书已开始下载')
  } catch {
    ElMessage.error('下载失败，请稍后重试')
  }
}

/** 重新审核：确认后重置页面状态并开始轮询 */
async function onReReview() {
  try {
    await ElMessageBox.confirm(
      `确认对申请「${report.value?.application_no || report.value?.report_no || report.value?.id}」重新发起审核吗？`,
      '重新审核',
      { type: 'warning', confirmButtonText: '重新审核', cancelButtonText: '取消' }
    )
  } catch {
    return // 用户取消
  }
  reReviewing.value = true
  try {
    await reReview(reportId)
    ElMessage.success('已提交重新审核')
    // 重置结果区，进入轮询
    review.value = null
    liveStatus.value = null
    if (report.value) {
      report.value.status = 'pending'
      report.value.error_msg = null
    }
    await loadTrial()
    startPolling()
  } catch {
    ElMessage.error('重新审核提交失败')
  } finally {
    reReviewing.value = false
  }
}

/** 新窗口打开 PDF */
function openPdfNewWindow() {
  if (pdfUrl.value) window.open(pdfUrl.value, '_blank')
}

/** 返回列表（保留筛选条件） */
function goBack() {
  router.back()
}

onMounted(async () => {
  try {
    await loadDetail()
    await loadVersions()
    await loadTrial()
    if (report.value?.status === 'done') {
      await Promise.all([loadReview(), loadCorrections()])
    } else if (isProcessing.value) {
      startPolling()
    }
    // 原文 PDF 与审核状态无关，并行加载
    loadPdf()
  } catch (e: unknown) {
    const status = (e as { response?: { status?: number } })?.response?.status
    loadError.value = status === 404 ? '报告不存在' : status === 403 ? '无权限查看该报告' : '报告加载失败'
  } finally {
    pageLoading.value = false
  }
})

onBeforeUnmount(() => {
  stopPolling()
  // 释放 blob URL，避免内存泄漏
  if (pdfUrl.value) URL.revokeObjectURL(pdfUrl.value)
})
</script>

<style scoped>
.action-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 16px;
  padding: 12px 20px;
}

.action-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.report-no {
  font-size: 16px;
  font-weight: 600;
}

.card-title {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 14px;
}

.info-card,
.version-card,
.trial-card,
.conclusion-card,
.review-card,
.processing-card,
.failed-card {
  margin-bottom: 16px;
}

.trial-card {
  border: 1px solid var(--el-color-warning-light-5);
  background: var(--el-color-warning-light-9);
}

.trial-title-row,
.trial-submit-row,
.trial-field {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.trial-form {
  display: grid;
  gap: 16px;
  margin-top: 16px;
}
.trial-audit-overview {
  display: grid;
  gap: 12px;
  padding: 14px;
  border: 1px solid #d9e2ef;
  border-radius: 10px;
  background: #f8fafc;
}
.trial-audit-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.trial-audit-heading p { margin: 5px 0 0; color: #64748b; line-height: 1.5; }
.trial-stat-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(92px, 1fr));
  gap: 8px;
}
.trial-stat-grid > div {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 10px 12px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  background: #fff;
}
.trial-stat-grid span { color: #64748b; font-size: 12px; }
.trial-stat-grid b { font-size: 19px; color: #0f172a; }
.trial-stat-grid .must b, .trial-stat-grid .danger b { color: #dc2626; }
.trial-stat-grid .manual b { color: #d97706; }
.trial-audit-collapse { background: #fff; border-radius: 8px; padding: 0 12px; }
.trial-review-checklist {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 18px;
}

.trial-label {
  color: var(--el-text-color-regular);
  font-weight: 600;
}

.trial-event-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.trial-event-grid label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  background: var(--el-bg-color);
}

.trial-progress {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.privacy-merge-summary {
  margin-top: 12px;
  padding: 9px 12px;
  border-radius: 7px;
  color: var(--el-text-color-regular);
  background: var(--el-fill-color-light);
  font-size: 13px;
}

.privacy-detail-list {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 8px;
  padding: 4px 18px 8px 48px;
}

.privacy-detail-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  min-width: 0;
}

.privacy-detail-item > div {
  min-width: 0;
  overflow-wrap: anywhere;
}

.privacy-detail-item p {
  margin: 3px 0 0;
  color: var(--el-text-color-secondary);
  line-height: 1.55;
}

.privacy-detail-index {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 22px;
  height: 22px;
  border-radius: 50%;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  font-size: 12px;
}

@media (max-width: 900px) {
  .trial-stat-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .trial-review-checklist { grid-template-columns: 1fr; }
  .trial-audit-heading { flex-direction: column; }
  .trial-event-grid { grid-template-columns: 1fr; }
}

.revision-inline {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.version-timeline {
  display: flex;
  gap: 10px;
  overflow-x: auto;
  padding-bottom: 2px;
}

.version-node {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: max-content;
  padding: 9px 12px;
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  background: var(--el-bg-color);
  color: var(--el-text-color-regular);
  cursor: pointer;
}

.version-node.active {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

.version-badge {
  font-weight: 700;
  color: var(--el-color-primary);
}

.revision-summary {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px 18px;
  margin-bottom: 14px;
}

.resolved-text { color: var(--el-color-success); }
.unresolved-text { color: var(--el-color-danger); }
.upload-copy { padding: 22px 0; font-weight: 600; }

/* ===== 处理中 ===== */
.processing-card {
  text-align: center;
  padding: 48px 20px;
}

.processing-icon {
  color: var(--ccc-primary);
}

.processing-title {
  font-size: 16px;
  font-weight: 600;
  margin-top: 12px;
}

.processing-sub {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin-top: 6px;
}

.processing-progress {
  width: min(520px, 90%);
  margin: 18px auto 0;
}

/* ===== 结论卡片 ===== */
.conclusion-body {
  display: flex;
  align-items: center;
  gap: 20px;
  flex-wrap: wrap;
}

.conclusion-tag {
  font-size: 18px;
  font-weight: 700;
  padding: 10px 22px;
  height: auto;
}

.conclusion-counts {
  display: flex;
  gap: 16px;
}

.count-item {
  font-size: 14px;
}

.count-item.must b {
  color: var(--el-color-danger);
  font-size: 18px;
}

.count-item.suggestion b {
  color: var(--el-color-warning);
  font-size: 18px;
}

/* ===== 需修改项表格 ===== */
.tab-tip {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 10px;
}

.sample-model {
  font-weight: 500;
}

.sample-spec {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.should-be {
  color: var(--el-color-danger);
  font-weight: 600;
}

.correction-alert {
  margin-bottom: 18px;
}

.full-width {
  width: 100%;
}

.original-item-box {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 18px;
  padding: 12px 14px;
  margin-bottom: 18px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-fill-color-light);
  font-size: 13px;
}

.original-item-box span {
  display: block;
  margin-bottom: 3px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

/* ===== 备注建议 ===== */
.remark-item {
  display: flex;
  gap: 12px;
  padding: 12px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  margin-bottom: 10px;
}

.remark-no {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--el-color-primary-light-9);
  color: var(--ccc-primary);
  font-size: 12px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.remark-text {
  line-height: 1.6;
}

.mt-12 {
  margin-top: 12px;
}

/* ===== Markdown 预览（v-html 内容需 :deep 穿透） ===== */
.md-preview {
  line-height: 1.8;
  font-size: 14px;
  overflow-x: auto;
  scrollbar-gutter: stable;
}

.md-preview :deep(h1),
.md-preview :deep(h2),
.md-preview :deep(h3) {
  margin: 16px 0 8px;
  font-weight: 600;
}

.md-preview :deep(table) {
  border-collapse: collapse;
  width: 100%;
  min-width: 980px;
  margin: 12px 0;
  font-size: 13px;
}

.md-preview :deep(th),
.md-preview :deep(td) {
  border: 1px solid var(--el-border-color);
  padding: 6px 10px;
  text-align: left;
  vertical-align: top;
  word-break: break-word;
}

.md-preview :deep(th) {
  background: var(--el-fill-color-light);
}

.md-preview :deep(code) {
  background: var(--el-fill-color-light);
  border-radius: 4px;
  padding: 1px 6px;
}

/* ===== PDF 面板 ===== */
.pdf-card {
  padding: 12px 16px;
}

.pdf-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.pdf-header .card-title {
  margin-bottom: 0;
}

.pdf-body {
  height: calc(100vh - 220px);
  min-height: 480px;
}

.pdf-center {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--el-text-color-secondary);
}

.pdf-frame {
  width: 100%;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}

/* 窄屏：上下布局时 PDF 高度收敛 */
@media (max-width: 1200px) {
  .pdf-body {
    height: 60vh;
  }
}

@media (max-width: 640px) {
  .original-item-box {
    grid-template-columns: 1fr;
  }
}
</style>
