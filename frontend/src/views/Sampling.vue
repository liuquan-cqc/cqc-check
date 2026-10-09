<template>
  <div class="sampling-page">
    <div class="page-intro">
      <div>
        <h1>下样管理</h1>
        <p>按已确认案例生成结构样品，支持合并材料覆盖、历史修改和固定文本复制。</p>
      </div>
      <div class="rule-state">
        <el-tag type="warning" effect="plain">开发审核稿</el-tag>
        <span>{{ catalog?.rule_package_version || '规则加载中' }}</span>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="sampling-tabs" @tab-change="onTabChange">
      <el-tab-pane label="新建下样" name="create">
        <div class="create-grid">
          <section class="input-panel ccc-card">
            <div class="panel-heading">
              <div><h2>申请范围</h2><p>只录入企业本次实际申请范围，不会扩展到标准最大范围。</p></div>
              <el-radio-group v-model="mode" @change="onModeChange">
                <el-radio-button value="fast">日常快速</el-radio-button>
                <el-radio-button value="merged">合并覆盖</el-radio-button>
              </el-radio-group>
            </div>

            <el-alert
              :title="mode === 'fast' ? '快速模式不计算材料供应商数量导致的增样。' : '多个申请必须属于同一家企业，结构分别计算，材料统一覆盖。'"
              type="info" :closable="false" show-icon
            />

            <div v-for="(application, appIndex) in applications" :key="application.key" class="application-block">
              <div class="application-title">
                <div><span>申请 {{ appIndex + 1 }}</span><strong>{{ application.unitGroups.length ? `已识别 ${application.unitGroups.length} 个产品单元` : '等待识别申请范围' }}</strong></div>
                <el-button v-if="mode === 'merged' && applications.length > 1" link type="danger" @click="removeApplication(appIndex)">移除</el-button>
              </div>
              <div class="application-fields">
                <el-form-item label="申请编号" required>
                  <el-input v-model="application.application_no" placeholder="请输入申请编号" />
                </el-form-item>
              </div>

              <div class="scope-paste-box">
                <div class="scope-paste-heading">
                  <div>
                    <strong>整段粘贴申请范围</strong>
                    <span>直接粘贴企业申请的型号规格，系统会自动识别并填入下方表格。</span>
                  </div>
                  <el-button type="primary" plain :loading="application.parsing" @click="parseScope(application)">识别并填入</el-button>
                </div>
                <el-input
                  v-model="application.rawScopeText"
                  type="textarea"
                  :rows="3"
                  resize="vertical"
                  placeholder="例：60227 IEC 74(RVVYP) 300/500V 0.5-2.5(2-36芯); 60227 IEC 75(RVVY) 300/500V 0.5-2.5(2-36芯);"
                  @paste="onScopePaste(application)"
                />
                <div class="scope-paste-tip">粘贴完成后会自动识别；也可以修改文字后再次点击“识别并填入”。</div>
                <div v-if="application.parseResult" class="parse-result">
                  <el-alert
                    :type="application.parseResult.can_apply && !application.parseResult.review_count && !application.parseResult.unrecognized_count ? 'success' : 'warning'"
                    :title="`已识别 ${application.parseResult.recognized_count} 条，需要确认 ${application.parseResult.review_count} 条，未识别 ${application.parseResult.unrecognized_count} 条`"
                    :closable="false"
                    show-icon
                  />
                  <div class="parse-entry-list">
                    <div v-for="(entry, entryIndex) in application.parseResult.entries" :key="`${entry.raw_text}-${entryIndex}`" class="parse-entry" :class="entry.status">
                      <span>{{ entry.status === 'recognized' ? '已识别' : entry.status === 'needs_review' ? '待确认' : '未识别' }}</span>
                      <div><strong>{{ entry.model ? `${entry.model} ${entry.voltage}` : entry.raw_text }}</strong><small v-if="entry.issues.length">{{ entry.issues.join('；') }}</small></div>
                    </div>
                  </div>
                  <div v-if="application.parseResult.issues.length" class="parse-global-issues">{{ application.parseResult.issues.join('；') }}</div>
                  <div v-if="application.parseResult.normalized_scope_text" class="normalized-scope-box">
                    <div class="normalized-scope-heading">
                      <div>
                        <strong>规整后的申请范围</strong>
                        <span>统一型号名称、电压、空格、区间符号和分号，不改变原申请的规格关系。</span>
                      </div>
                      <el-button :icon="CopyDocument" type="primary" plain @click="copyText(application.parseResult.normalized_scope_text, '规整范围已复制')">复制规整范围</el-button>
                    </div>
                    <el-input
                      :model-value="application.parseResult.normalized_scope_text"
                      type="textarea"
                      :rows="Math.min(10, Math.max(2, application.parseResult.normalized_scope_text.split('\n').length))"
                      readonly
                    />
                    <div v-if="application.parseResult.unrecognized_count" class="normalized-scope-warning">未识别条目已原样保留在复制文本中，请人工确认后再使用。</div>
                  </div>
                </div>
              </div>

              <div v-for="(unitGroup, unitIndex) in application.unitGroups" :key="unitGroup.key" class="unit-result-block">
                <div class="unit-result-heading">
                  <div><el-tag effect="plain">单元{{ Number(unitGroup.unit_code) || '?' }}</el-tag><strong>{{ unitName(unitGroup.unit_code) || '等待人工选择产品单元' }}</strong></div>
                  <div>
                    <el-button v-if="!unitGroup.editingUnit" link type="primary" @click="unitGroup.editingUnit = true">纠正单元</el-button>
                    <el-button v-if="application.unitGroups.length > 1" link type="danger" @click="removeUnitGroup(application, unitIndex)">移除分组</el-button>
                  </div>
                </div>

                <el-form-item v-if="unitGroup.editingUnit || !unitGroup.unit_code" label="人工纠正产品单元">
                  <el-select v-model="unitGroup.unit_code" placeholder="仅在自动识别不正确时选择" filterable @change="onUnitChange(unitGroup)">
                    <el-option v-for="unit in catalog?.units || []" :key="unit.unit_code" :label="`单元${Number(unit.unit_code)} ${unit.unit_name}`" :value="unit.unit_code" />
                  </el-select>
                </el-form-item>

                <el-form-item label="申请型号" required>
                  <el-select v-model="unitGroup.selectedModelRefs" multiple filterable collapse-tags :max-collapse-tags="3" placeholder="选择本单元申请型号" @change="syncModelRanges(unitGroup)">
                    <el-option v-for="model in unitModels(unitGroup.unit_code)" :key="model.id" :label="`${model.display_name} ${model.voltage}`" :value="model.id" />
                  </el-select>
                </el-form-item>

                <div v-if="unitGroup.modelRanges.length" class="range-table">
                  <div class="range-head"><span>型号</span><span>截面范围 mm²</span><span>芯数范围</span><span>形状</span></div>
                  <div v-for="range in unitGroup.modelRanges" :key="range.model_ref" class="range-row">
                    <div class="model-cell"><strong>{{ modelByRef(range.model_ref)?.display_name }}</strong><small>{{ modelByRef(range.model_ref)?.voltage }}</small></div>
                    <div class="range-pair">
                      <el-input-number v-model="range.section_min" :min="0.01" :controls="false" placeholder="最小" />
                      <span>至</span>
                      <el-input-number v-model="range.section_max" :min="0.01" :controls="false" placeholder="最大" />
                    </div>
                    <div class="range-pair">
                      <el-input-number v-model="range.core_min" :min="1" :precision="0" :controls="false" placeholder="最少" />
                      <span>至</span>
                      <el-input-number v-model="range.core_max" :min="1" :precision="0" :controls="false" placeholder="最多" />
                    </div>
                    <el-select v-model="range.shapes" multiple collapse-tags placeholder="全部">
                      <el-option v-for="shape in modelShapes(range.model_ref)" :key="shape" :label="shapeText(shape)" :value="shape" />
                    </el-select>
                  </div>
                </div>
              </div>

              <el-button v-if="!application.unitGroups.length" class="manual-unit-button" plain @click="addManualUnit(application)">未识别时人工选择产品单元</el-button>
            </div>

            <el-button v-if="mode === 'merged'" class="add-application" :icon="Plus" @click="addApplication">增加申请编号</el-button>

            <section v-if="mode === 'merged'" class="material-section">
              <div class="subheading"><div><h3>材料覆盖数量</h3><p>可上传产品描述自动预填，也可直接人工录入和调整。</p></div><el-button link type="primary" :icon="Plus" @click="addMaterialGroup">增加材料组</el-button></div>
              <div class="material-import-box">
                <div class="material-import-copy">
                  <strong>上传产品描述自动识别</strong>
                  <span>支持PDF、DOCX、TXT，可一次上传多份。识别结果只预填下方材料组，生成前仍可人工修改。</span>
                </div>
                <el-upload
                  v-model:file-list="materialFiles"
                  drag multiple :auto-upload="false" :limit="10"
                  accept=".pdf,.docx,.txt"
                  :on-exceed="onMaterialFileExceed"
                >
                  <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
                  <div class="el-upload__text">拖入产品描述，或<em>点击选择</em></div>
                  <template #tip><div class="el-upload__tip">单个文件不超过15MB；扫描件会使用本地OCR并标记人工确认。</div></template>
                </el-upload>
                <div class="material-import-actions">
                  <el-button type="primary" plain :loading="materialRecognizing" :disabled="!materialFiles.length" @click="recognizeMaterialFiles">识别并填入材料组</el-button>
                  <span>重新识别仅替换上次自动识别项，不删除人工新增项。</span>
                </div>
                <div v-if="materialRecognition" class="material-recognition-result">
                  <el-alert
                    :type="materialRecognition.review_count ? 'warning' : 'success'"
                    :title="`已识别 ${materialRecognition.group_count} 个材料组，${materialRecognition.review_count} 个需人工确认`"
                    :closable="false" show-icon
                  />
                  <div class="recognized-documents">
                    <el-tag v-for="doc in materialRecognition.documents" :key="doc.filename" effect="plain">{{ doc.filename }} · {{ extractionMethodText(doc.extraction_method) }}</el-tag>
                  </div>
                  <div v-if="materialRecognition.warnings.length" class="recognition-warnings">
                    <div v-for="warning in materialRecognition.warnings" :key="warning">{{ warning }}</div>
                  </div>
                </div>
              </div>
              <div v-if="!materialGroups.length" class="inline-empty">尚未录入材料组，仍可先预览结构方案。</div>
              <div v-for="(group, index) in materialGroups" :key="group.key" class="material-row">
                <div class="material-name-field">
                  <el-input v-model="group.group_code" placeholder="材料组名称，如J-70 绝缘" />
                  <div v-if="group.recognition_source" class="material-source-line">
                    <el-tag size="small" :type="group.needs_review ? 'warning' : 'success'">{{ group.needs_review ? '待确认' : '自动识别' }}</el-tag>
                    <span>{{ group.recognition_source }}</span>
                  </div>
                  <div v-if="group.suppliers?.length" class="supplier-preview">
                    <span v-for="supplier in group.suppliers" :key="supplier.name">{{ supplier.name }}<em v-if="supplier.cqc_numbers?.length">（{{ supplier.cqc_numbers.join('、') }}）</em></span>
                  </div>
                </div>
                <el-select v-model="group.category" placeholder="材料类别">
                  <el-option v-for="option in materialCategories" :key="option.value" :label="option.label" :value="option.value" />
                </el-select>
                <label><span>覆盖项</span><el-input-number v-model="group.total_items" :min="0" :precision="0" /></label>
                <label><span>CQC备案</span><el-input-number v-model="group.cqc_filed_items" :min="0" :max="group.total_items" :precision="0" /></label>
                <el-button link type="danger" @click="materialGroups.splice(index, 1)">删除</el-button>
              </div>
            </section>

            <div class="form-actions">
              <el-button :loading="previewing" @click="onPreview">预览方案</el-button>
              <el-button type="primary" :loading="saving" @click="onSaveTask">保存到下样历史</el-button>
            </div>
          </section>

          <aside class="result-panel ccc-card">
            <div class="panel-heading compact"><div><h2>送样清单</h2><p v-if="result">共 {{ result.samples.length }} 件，{{ result.validation_status === 'draft_only' ? '仅可保存草稿' : '覆盖完整' }}</p></div><el-button v-if="result" :icon="CopyDocument" @click="copyText(result.copy_text)">复制</el-button></div>
            <template v-if="result">
              <el-alert v-for="warning in result.warnings" :key="warning" :title="warning" type="warning" :closable="false" show-icon class="result-alert" />
              <div class="sample-list">
                <article v-for="sample in result.samples" :key="sample.id" class="sample-item">
                  <div class="sample-index">{{ sample.id }}</div>
                  <div><strong>{{ sample.model }} {{ sample.voltage }}</strong><p>{{ sample.specification }}</p><small>{{ sample.reasons.join('；') }}</small></div>
                </article>
              </div>
              <el-collapse class="analysis-collapse">
                <el-collapse-item title="查看规则覆盖与材料分配" name="analysis">
                  <div v-for="sample in result.samples" :key="`analysis-${sample.id}`" class="analysis-row">
                    <strong>{{ sample.id }} {{ sample.model }}</strong>
                    <span>规则：{{ sample.rule_refs.join('、') || '人工样品' }}</span>
                    <span v-if="sample.material_coverage.length">材料：{{ sample.material_coverage.join('、') }}</span>
                  </div>
                </el-collapse-item>
              </el-collapse>
              <el-input :model-value="result.copy_text" type="textarea" :rows="Math.min(14, result.samples.length + 3)" readonly class="copy-area" />
              <div v-if="savedTaskNo" class="saved-hint"><el-icon><CircleCheckFilled /></el-icon>已保存 {{ savedTaskNo }}</div>
            </template>
            <el-empty v-else description="填写申请范围后预览送样清单" :image-size="92" />
          </aside>
        </div>
      </el-tab-pane>

      <el-tab-pane label="下样历史" name="history">
        <section class="history-card ccc-card">
          <div class="history-toolbar">
            <el-input v-model="historyKeyword" :prefix-icon="Search" clearable placeholder="搜索任务编号或申请编号" @keyup.enter="loadHistory" @clear="loadHistory" />
            <el-button :icon="Refresh" :loading="historyLoading" @click="loadHistory">刷新</el-button>
          </div>
          <el-table v-loading="historyLoading" :data="history" stripe @row-click="openTask">
            <el-table-column prop="task_no" label="任务编号" min-width="190" />
            <el-table-column label="申请编号" min-width="200"><template #default="{ row }">{{ row.application_nos.join('、') }}</template></el-table-column>
            <el-table-column label="模式" width="105"><template #default="{ row }"><el-tag effect="plain">{{ row.mode === 'fast' ? '日常快速' : '合并覆盖' }}</el-tag></template></el-table-column>
            <el-table-column label="当前版本" width="95" align="center"><template #default="{ row }">V{{ row.current_version_no }}</template></el-table-column>
            <el-table-column prop="sample_count" label="样品数" width="85" align="center" />
            <el-table-column label="状态" width="120"><template #default="{ row }"><el-tag :type="row.hard_gap_count ? 'danger' : 'warning'">{{ row.hard_gap_count ? `${row.hard_gap_count}项缺口` : '审核稿' }}</el-tag></template></el-table-column>
            <el-table-column label="更新时间" width="170"><template #default="{ row }">{{ formatTime(row.updated_at) }}</template></el-table-column>
            <el-table-column label="操作" width="145" fixed="right"><template #default="{ row }"><el-button link type="primary" @click.stop="openTask(row)">查看</el-button><el-button v-if="auth.hasRole('admin')" link type="danger" :icon="Delete" :loading="deletingTaskId === row.id" @click.stop="removeTask(row)">删除</el-button></template></el-table-column>
            <template #empty><el-empty description="暂无下样历史" /></template>
          </el-table>
        </section>
      </el-tab-pane>
    </el-tabs>

    <el-drawer v-model="detailVisible" title="下样任务详情" size="min(900px, 92vw)" destroy-on-close>
      <template v-if="taskDetail">
        <div class="detail-summary">
          <div><span>任务编号</span><strong>{{ taskDetail.task_no }}</strong></div><div><span>申请编号</span><strong>{{ taskDetail.application_nos.join('、') }}</strong></div>
        </div>
        <div class="version-bar">
          <el-select v-model="selectedVersionId" placeholder="选择历史版本">
            <el-option v-for="version in taskDetail.versions" :key="version.id" :label="`V${version.version_no} ${version.source === 'manual' ? '人工调整' : '规则生成'}`" :value="version.id" />
          </el-select>
          <div><el-button :icon="CopyDocument" @click="copyText(selectedVersion?.copy_text || '')">复制文本</el-button><el-button type="primary" :icon="EditPen" @click="startEdit">修改当前方案</el-button></div>
        </div>
        <el-alert v-if="selectedVersion?.hard_gap_count" :title="`该版本存在 ${selectedVersion.hard_gap_count} 项硬性缺口，只能保存草稿。`" type="error" :closable="false" show-icon />
        <el-table :data="selectedVersion?.plan?.samples || []" class="detail-table">
          <el-table-column prop="id" label="序号" width="70" />
          <el-table-column label="型号规格" min-width="300"><template #default="{ row }"><strong>{{ row.model }} {{ row.voltage }}</strong><div>{{ row.specification }}</div></template></el-table-column>
          <el-table-column label="覆盖原因" min-width="320"><template #default="{ row }">{{ (row.reasons || []).join('；') }}</template></el-table-column>
        </el-table>
        <el-input :model-value="selectedVersion?.copy_text || ''" type="textarea" :rows="10" readonly class="copy-area" />
        <div class="version-history"><h3>版本记录</h3><el-timeline><el-timeline-item v-for="version in taskDetail.versions" :key="version.id" :timestamp="formatTime(version.created_at)" placement="top"><strong>V{{ version.version_no }} {{ version.source === 'manual' ? '人工调整' : '规则生成' }}</strong><p>{{ version.change_reason }}</p><small>{{ version.creator_name }}，样品 {{ version.plan?.samples?.length || 0 }} 件</small></el-timeline-item></el-timeline></div>
      </template>
    </el-drawer>

    <el-dialog v-model="editVisible" title="修改下样方案" width="min(1100px, 94vw)" destroy-on-close>
      <el-alert title="保存会创建新的历史版本，不会覆盖原方案。存在硬性缺口时仍可保存草稿。" type="info" :closable="false" show-icon />
      <el-table :data="editSamples" class="edit-table">
        <el-table-column label="型号" min-width="230"><template #default="{ row }"><strong>{{ row.model }}</strong><small>{{ row.voltage }}</small></template></el-table-column>
        <el-table-column label="截面" width="125"><template #default="{ row }"><el-input-number v-model="row.section" :min="0.01" :controls="false" /></template></el-table-column>
        <el-table-column label="芯数" width="115"><template #default="{ row }"><el-input-number v-model="row.cores" :min="1" :precision="0" :controls="false" /></template></el-table-column>
        <el-table-column label="颜色" width="115"><template #default="{ row }"><el-select v-model="row.color" clearable><el-option label="白色" value="white" /><el-option label="黑色" value="black" /></el-select></template></el-table-column>
        <el-table-column label="形状" width="120"><template #default="{ row }"><el-select v-model="row.shape"><el-option v-for="shape in modelShapes(row.model_ref)" :key="shape" :label="shapeText(shape)" :value="shape" /></el-select></template></el-table-column>
        <el-table-column label="操作" width="145"><template #default="{ row, $index }"><el-button link type="primary" @click="duplicateSample(row)">复制一件</el-button><el-button link type="danger" @click="editSamples.splice($index, 1)">删除</el-button></template></el-table-column>
      </el-table>
      <el-form label-position="top" class="reason-form"><el-form-item label="修改原因" required><el-input v-model="editReason" type="textarea" :rows="3" placeholder="例：企业指定送某规格，增加一件用于材料覆盖" maxlength="1000" show-word-limit /></el-form-item></el-form>
      <template #footer><el-button @click="editVisible = false">取消</el-button><el-button type="primary" :loading="versionSaving" @click="saveManualVersion">保存新版本</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type UploadUserFile } from 'element-plus'
import { CircleCheckFilled, CopyDocument, Delete, EditPen, Plus, Refresh, Search, UploadFilled } from '@element-plus/icons-vue'
import { formatSystemTime } from '@/utils/datetime'
import { useAppStore } from '@/stores/app'
import { useAuthStore } from '@/stores/auth'
import {
  createSamplingTask, createSamplingVersion, deleteSamplingTask, getSamplingCatalog, getSamplingTask, getSamplingTasks, parseSamplingScope, previewSampling, recognizeSamplingMaterials,
  type SamplingCatalog, type SamplingMaterialRecognitionResult, type SamplingModel, type SamplingResult, type SamplingSample, type SamplingScopeParseResult, type SamplingTaskListItem
} from '@/api/sampling'

interface ModelRange { model_ref: string; section_min: number | null; section_max: number | null; core_min: number | null; core_max: number | null; shapes: string[]; include_special: boolean; scope_expression: string }
interface UnitGroupForm { key: number; unit_code: string; selectedModelRefs: string[]; modelRanges: ModelRange[]; editingUnit: boolean }
interface ApplicationForm { key: number; application_no: string; unitGroups: UnitGroupForm[]; rawScopeText: string; parseResult: SamplingScopeParseResult | null; parsing: boolean }

const app = useAppStore()
const auth = useAuthStore()
const activeTab = ref('create')
const catalog = ref<SamplingCatalog | null>(null)
const mode = ref<'fast' | 'merged'>('fast')
let applicationKey = 1
let unitGroupKey = 1
const applications = reactive<ApplicationForm[]>([newApplication()])
const materialGroups = reactive<any[]>([])
const materialFiles = ref<UploadUserFile[]>([])
const materialRecognition = ref<SamplingMaterialRecognitionResult | null>(null)
const materialRecognizing = ref(false)
const result = ref<SamplingResult | null>(null)
const previewing = ref(false)
const saving = ref(false)
const savedTaskNo = ref('')

const history = ref<SamplingTaskListItem[]>([])
const historyKeyword = ref('')
const historyLoading = ref(false)
const deletingTaskId = ref<number | null>(null)
const detailVisible = ref(false)
const taskDetail = ref<any>(null)
const selectedVersionId = ref<number | null>(null)
const editVisible = ref(false)
const editSamples = ref<SamplingSample[]>([])
const editReason = ref('')
const versionSaving = ref(false)

const materialCategories = [
  { label: '铜导体', value: 'copper_conductor' }, { label: '铝导体', value: 'aluminum_conductor' },
  { label: '普通绝缘', value: 'insulation_normal' }, { label: '90℃绝缘', value: 'insulation_90' },
  { label: '普通护套', value: 'sheath_normal' }, { label: '90℃护套', value: 'sheath_90' },
  { label: '外编织层 YG', value: 'outer_braid' }, { label: '其他人工兼容组', value: 'other' }
]

const allModels = computed(() => (catalog.value?.units || []).flatMap(unit => unit.models))
const selectedVersion = computed(() => taskDetail.value?.versions?.find((row: any) => row.id === selectedVersionId.value))

function newApplication(): ApplicationForm { return { key: applicationKey++, application_no: '', unitGroups: [], rawScopeText: '', parseResult: null, parsing: false } }
function newUnitGroup(unitCode = '', models: Array<Record<string, any>> = []): UnitGroupForm {
  return {
    key: unitGroupKey++,
    unit_code: unitCode,
    selectedModelRefs: models.map(row => row.model_ref),
    modelRanges: models.map(row => ({
      model_ref: row.model_ref,
      section_min: row.section_min ?? null,
      section_max: row.section_max ?? null,
      core_min: row.core_min ?? null,
      core_max: row.core_max ?? null,
      shapes: row.shapes || [],
      include_special: Boolean(row.include_special),
      scope_expression: row.scope_expression || ''
    })),
    editingUnit: !unitCode
  }
}
function unitModels(code: string): SamplingModel[] { return catalog.value?.units.find(unit => unit.unit_code === code)?.models || [] }
function unitName(code: string): string { return catalog.value?.units.find(unit => unit.unit_code === code)?.unit_name || '' }
function modelByRef(ref: string): SamplingModel | undefined { return allModels.value.find(model => model.id === ref) }
function shapeText(shape: string): string { return ({ round: '圆形', flat: '扁形', twisted: '绞合', parallel: '平行', grouped: '成组', special: '特殊结构' } as Record<string, string>)[shape] || shape }
function modelShapes(ref: string): string[] { return [...new Set((modelByRef(ref)?.specification_groups || []).flatMap(group => group.shapes || []))] }

function bounds(model: SamplingModel, field: 'sections' | 'cores'): [number | null, number | null] {
  const values: number[] = []
  for (const group of model.specification_groups || []) {
    const spec = group[field] || {}
    if (Array.isArray(spec.values)) values.push(...spec.values)
    if (typeof spec.min === 'number') values.push(spec.min)
    if (typeof spec.max === 'number') values.push(spec.max)
  }
  return values.length ? [Math.min(...values), Math.max(...values)] : [null, null]
}

function onModeChange() {
  if (mode.value === 'fast' && applications.length > 1) applications.splice(1)
  result.value = null; savedTaskNo.value = ''
}
function addApplication() { applications.push(newApplication()) }
function removeApplication(index: number) { applications.splice(index, 1); result.value = null }
function addManualUnit(application: ApplicationForm) { application.unitGroups.push(newUnitGroup()) }
function removeUnitGroup(application: ApplicationForm, index: number) { application.unitGroups.splice(index, 1); result.value = null }
function onUnitChange(unitGroup: UnitGroupForm) { unitGroup.selectedModelRefs = []; unitGroup.modelRanges = []; unitGroup.editingUnit = false; result.value = null }
function syncModelRanges(unitGroup: UnitGroupForm) {
  const existing = new Map(unitGroup.modelRanges.map(row => [row.model_ref, row]))
  unitGroup.modelRanges = unitGroup.selectedModelRefs.map(ref => {
    if (existing.has(ref)) return existing.get(ref)!
    const model = modelByRef(ref)!
    const sections = bounds(model, 'sections'); const cores = bounds(model, 'cores')
    return { model_ref: ref, section_min: sections[0], section_max: sections[1], core_min: cores[0], core_max: cores[1], shapes: modelShapes(ref), include_special: false, scope_expression: '' }
  })
  result.value = null
}
function addMaterialGroup() { materialGroups.push({ key: Date.now() + Math.random(), group_code: '', category: '', compatibility_group: '', total_items: 0, cqc_filed_items: 0, model_refs: [] }) }
function extractionMethodText(method: string): string { return ({ pdf_text: 'PDF文字层', local_ocr: '本地OCR', docx_text: 'Word文字', plain_text: '文本' } as Record<string, string>)[method] || '未提取' }
function onMaterialFileExceed() { ElMessage.warning('一次最多上传10份产品描述') }
async function recognizeMaterialFiles() {
  const files: File[] = []
  materialFiles.value.forEach(item => { if (item.raw) files.push(item.raw as File) })
  if (!files.length) { ElMessage.warning('请先选择产品描述'); return }
  materialRecognizing.value = true
  try {
    const recognized = await recognizeSamplingMaterials(files)
    materialRecognition.value = recognized
    const manualGroups = materialGroups.filter(group => !group.recognition_source)
    const automaticGroups = recognized.groups.map(group => ({
      key: Date.now() + Math.random(),
      group_code: group.group_code,
      category: group.category,
      compatibility_group: group.compatibility_group,
      total_items: group.total_items,
      cqc_filed_items: group.cqc_filed_items,
      model_refs: group.model_refs || [],
      suppliers: group.suppliers || [],
      material_brands: group.material_brands || [],
      recognition_notes: group.needs_review ? ['自动识别结果需人工确认'] : [],
      recognition_source: group.source,
      needs_review: group.needs_review
    }))
    materialGroups.splice(0, materialGroups.length, ...manualGroups, ...automaticGroups)
    result.value = null; savedTaskNo.value = ''
    if (recognized.review_count || recognized.warnings.length) ElMessage.warning('已填入识别结果，请确认标黄项和数量')
    else ElMessage.success(`已自动填入 ${recognized.group_count} 个材料组`)
  } finally { materialRecognizing.value = false }
}

async function parseScope(application: ApplicationForm, automatic = false) {
  if (application.parsing) return
  if (!application.rawScopeText.trim()) {
    if (!automatic) ElMessage.warning('请先粘贴申请型号规格')
    return
  }
  application.parsing = true
  try {
    const parsed = await parseSamplingScope(application.rawScopeText)
    application.parseResult = parsed
    if (!parsed.can_apply) {
      ElMessage.warning(parsed.issues[0] || '识别结果不能自动填入，请检查粘贴内容')
      return
    }
    application.unitGroups = parsed.groups.map(group => newUnitGroup(group.unit_code, group.models))
    result.value = null
    savedTaskNo.value = ''
    if (parsed.review_count || parsed.unrecognized_count) {
      ElMessage.warning(`已填入 ${parsed.recognized_count} 条，请确认标黄色的识别结果`)
    } else {
      ElMessage.success(`已自动识别 ${parsed.recognized_count} 条，并按 ${parsed.groups.length} 个产品单元分组`)
    }
  } finally {
    application.parsing = false
  }
}

function onScopePaste(application: ApplicationForm) {
  window.setTimeout(() => parseScope(application, true), 0)
}

function payload() {
  return {
    mode: mode.value,
    applications: applications.flatMap(application => application.unitGroups.map(unitGroup => ({ application_no: application.application_no.trim(), unit_code: unitGroup.unit_code, models: unitGroup.modelRanges, raw_scope_text: application.rawScopeText.trim() }))),
    material_groups: materialGroups.map(({ key, ...row }) => row)
  }
}
function validateInput(): boolean {
  for (const application of applications) {
    if (!application.application_no.trim()) { ElMessage.warning('请输入申请编号'); return false }
    if (!application.unitGroups.length) { ElMessage.warning('请粘贴申请范围并完成产品单元识别'); return false }
    if (application.unitGroups.some(group => !group.unit_code || !group.modelRanges.length)) { ElMessage.warning('请确认每个自动识别单元及其申请型号'); return false }
  }
  if (materialGroups.some(group => !group.group_code.trim() || !group.category)) { ElMessage.warning('请补全材料组名称和类别'); return false }
  return true
}
async function onPreview() {
  if (!validateInput()) return
  previewing.value = true
  try { result.value = await previewSampling(payload()); savedTaskNo.value = '' } finally { previewing.value = false }
}
async function onSaveTask() {
  if (!validateInput()) return
  saving.value = true
  try { const response = await createSamplingTask(payload()); result.value = response.result; savedTaskNo.value = response.task_no; ElMessage.success('已保存到下样历史') } finally { saving.value = false }
}
async function copyText(text: string, successMessage = '送样清单已复制') {
  if (!text) return
  try {
    let copied = false
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text)
        copied = true
      } catch {
        // 内网浏览器可能暴露 Clipboard API 但拒绝写入，继续使用同步备用方案。
      }
    }
    if (!copied) {
      const textarea = document.createElement('textarea')
      textarea.value = text
      textarea.setAttribute('readonly', '')
      textarea.style.position = 'fixed'
      textarea.style.top = '0'
      textarea.style.left = '0'
      textarea.style.width = '2px'
      textarea.style.height = '2px'
      textarea.style.padding = '0'
      textarea.style.border = '0'
      textarea.style.outline = '0'
      textarea.style.background = 'transparent'
      document.body.appendChild(textarea)
      textarea.focus({ preventScroll: true })
      textarea.select()
      textarea.setSelectionRange(0, textarea.value.length)
      copied = document.execCommand('copy')
      document.body.removeChild(textarea)
      if (!copied) throw new Error('browser copy command failed')
    }
    ElMessage.success(successMessage)
  } catch {
    ElMessage.error('复制失败，请在文本框中全选后手动复制')
  }
}

async function onTabChange(name: string | number) { if (name === 'history') await loadHistory() }
async function loadHistory() { historyLoading.value = true; try { history.value = await getSamplingTasks(historyKeyword.value) } finally { historyLoading.value = false } }
async function openTask(row: SamplingTaskListItem) { taskDetail.value = await getSamplingTask(row.id); selectedVersionId.value = taskDetail.value.current_version_id; detailVisible.value = true }
async function removeTask(row: SamplingTaskListItem) {
  try {
    await ElMessageBox.confirm(
      `删除任务“${row.task_no}”后，其全部 ${row.current_version_no || 0} 个历史方案版本将一并删除且无法恢复。确认删除吗？`,
      '删除下样任务',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' }
    )
  } catch { return }
  deletingTaskId.value = row.id
  try {
    await deleteSamplingTask(row.id)
    if (taskDetail.value?.id === row.id) { detailVisible.value = false; taskDetail.value = null }
    await loadHistory()
    ElMessage.success('下样任务及其全部版本已删除')
  } finally { deletingTaskId.value = null }
}
function startEdit() { if (!selectedVersion.value) return; editSamples.value = JSON.parse(JSON.stringify(selectedVersion.value.plan.samples || [])); editReason.value = ''; editVisible.value = true }
function duplicateSample(row: SamplingSample) { const copy = JSON.parse(JSON.stringify(row)); copy.id = undefined; copy.reasons = [...(copy.reasons || []), '人工复制样品']; editSamples.value.push(copy) }
async function saveManualVersion() {
  if (!editReason.value.trim()) { ElMessage.warning('请填写修改原因'); return }
  if (!editSamples.value.length) { ElMessage.warning('方案至少保留一件样品'); return }
  versionSaving.value = true
  try {
    const samples = editSamples.value.map(row => ({ ...row, any_spec: false, specification: '' }))
    await createSamplingVersion(taskDetail.value.id, { samples, change_type: 'mixed', reason: editReason.value.trim() })
    taskDetail.value = await getSamplingTask(taskDetail.value.id); selectedVersionId.value = taskDetail.value.current_version_id; editVisible.value = false; ElMessage.success('已创建新的方案版本'); await loadHistory()
  } finally { versionSaving.value = false }
}
function formatTime(value?: string) { return formatSystemTime(value, app.systemConfig.timezone) }

onMounted(async () => { catalog.value = await getSamplingCatalog() })
</script>

<style scoped>
.sampling-page { max-width: 1680px; margin: 0 auto; }
.page-intro { display:flex; align-items:flex-start; justify-content:space-between; gap:20px; margin-bottom:14px; }
.page-intro h1 { margin:0 0 6px; font-size:22px; }
.page-intro p, .panel-heading p, .subheading p { margin:0; color:var(--el-text-color-secondary); line-height:1.55; }
.rule-state { display:flex; align-items:center; gap:10px; color:var(--el-text-color-secondary); font-size:13px; }
.sampling-tabs :deep(.el-tabs__header) { margin-bottom:16px; }
.create-grid { display:grid; grid-template-columns:minmax(620px, 1.35fr) minmax(420px, .85fr); gap:16px; align-items:start; }
.input-panel, .result-panel, .history-card { border:1px solid var(--el-border-color-lighter); box-shadow:none; }
.result-panel { position:sticky; top:0; max-height:calc(100vh - 96px); overflow:auto; }
.panel-heading, .subheading { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; margin-bottom:16px; }
.panel-heading.compact { align-items:center; }
.panel-heading h2, .subheading h3 { margin:0 0 5px; font-size:17px; }
.application-block { margin-top:16px; padding:16px; border:1px solid var(--el-border-color); border-radius:10px; background:var(--el-fill-color-blank); }
.application-title { display:flex; align-items:center; justify-content:space-between; margin-bottom:14px; }
.application-title div { display:flex; align-items:center; gap:10px; }.application-title span { color:var(--el-text-color-secondary); }
.application-fields { display:grid; grid-template-columns:1fr; gap:14px; }.application-fields :deep(.el-form-item), .application-block :deep(.el-form-item) { margin-bottom:12px; }
.scope-paste-box { margin:2px 0 16px; padding:14px; border:1px solid var(--el-color-primary-light-7); border-radius:9px; background:var(--el-color-primary-light-9); }.scope-paste-heading { display:flex; align-items:flex-start; justify-content:space-between; gap:14px; margin-bottom:10px; }.scope-paste-heading strong, .scope-paste-heading span { display:block; }.scope-paste-heading span, .scope-paste-tip { color:var(--el-text-color-secondary); font-size:12px; line-height:1.5; }.scope-paste-heading span { margin-top:4px; }.scope-paste-tip { margin-top:6px; }.parse-result { margin-top:10px; }.parse-entry-list { display:grid; gap:6px; margin-top:8px; }.parse-entry { display:grid; grid-template-columns:54px 1fr; gap:9px; padding:8px 10px; border-radius:7px; background:var(--el-fill-color-blank); }.parse-entry > span { color:var(--el-color-success); font-size:12px; }.parse-entry.needs_review > span, .parse-entry.unrecognized > span { color:var(--el-color-warning); }.parse-entry strong, .parse-entry small { display:block; }.parse-entry small { margin-top:3px; color:var(--el-text-color-secondary); line-height:1.45; }
.parse-global-issues { margin-top:7px; color:var(--el-text-color-secondary); font-size:12px; line-height:1.5; }.unit-result-block { margin-top:12px; padding:14px; border:1px solid var(--el-border-color-lighter); border-radius:9px; background:var(--el-bg-color); }.unit-result-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:12px; }.unit-result-heading > div { display:flex; align-items:center; gap:9px; }.unit-result-heading strong { font-size:14px; }.manual-unit-button { width:100%; border-style:dashed; }
.normalized-scope-box { margin-top:12px; padding:12px; border:1px solid var(--el-border-color-lighter); border-radius:8px; background:var(--el-fill-color-blank); }.normalized-scope-heading { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:9px; }.normalized-scope-heading strong, .normalized-scope-heading span { display:block; }.normalized-scope-heading span, .normalized-scope-warning { margin-top:3px; color:var(--el-text-color-secondary); font-size:12px; line-height:1.5; }.normalized-scope-box :deep(textarea) { font-family:'Source Han Sans SC','PingFang SC',sans-serif; line-height:1.65; }.normalized-scope-warning { color:var(--el-color-warning); }
.range-table { border:1px solid var(--el-border-color-lighter); border-radius:8px; overflow:hidden; }
.range-head, .range-row { display:grid; grid-template-columns:minmax(155px, 1.1fr) minmax(190px, 1fr) minmax(170px, .85fr) minmax(120px, .7fr); gap:10px; align-items:center; padding:10px 12px; }
.range-head { background:var(--el-fill-color-light); color:var(--el-text-color-secondary); font-size:12px; }.range-row + .range-row { border-top:1px solid var(--el-border-color-lighter); }
.model-cell { min-width:0; }.model-cell strong, .model-cell small, .edit-table small { display:block; }.model-cell strong { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }.model-cell small, .edit-table small { color:var(--el-text-color-secondary); margin-top:3px; }
.range-pair { display:grid; grid-template-columns:1fr auto 1fr; align-items:center; gap:6px; }.range-pair :deep(.el-input-number) { width:100%; }
.add-application { width:100%; margin-top:14px; border-style:dashed; }
.material-section { margin-top:18px; padding-top:18px; border-top:1px solid var(--el-border-color-lighter); }
.material-import-box { margin-bottom:14px; padding:14px; border:1px solid var(--el-color-primary-light-7); border-radius:10px; background:var(--el-color-primary-light-9); }
.material-import-copy { margin-bottom:10px; }.material-import-copy strong, .material-import-copy span { display:block; }.material-import-copy span { margin-top:4px; color:var(--el-text-color-secondary); font-size:12px; line-height:1.55; }
.material-import-box :deep(.el-upload-dragger) { padding:16px; background:rgba(255,255,255,.72); }.material-import-box :deep(.el-icon--upload) { margin-bottom:5px; font-size:30px; }.material-import-box :deep(.el-upload__tip) { margin-top:5px; }
.material-import-actions { display:flex; align-items:center; gap:12px; margin-top:10px; }.material-import-actions span { color:var(--el-text-color-secondary); font-size:12px; line-height:1.45; }
.material-recognition-result { display:grid; gap:9px; margin-top:12px; }.recognized-documents { display:flex; flex-wrap:wrap; gap:6px; }.recognition-warnings { padding:9px 11px; color:var(--el-color-warning-dark-2); font-size:12px; line-height:1.6; border-radius:7px; background:var(--el-color-warning-light-9); }
.material-row { display:grid; grid-template-columns:minmax(210px,1.25fr) minmax(145px,1fr) 130px 130px 46px; gap:10px; align-items:end; margin-top:10px; padding:10px; border:1px solid var(--el-border-color-lighter); border-radius:8px; }.material-row label span { display:block; margin-bottom:5px; font-size:12px; color:var(--el-text-color-secondary); }.material-row :deep(.el-input-number) { width:100%; }
.material-name-field { min-width:0; }.material-source-line { display:flex; align-items:center; gap:6px; margin-top:6px; color:var(--el-text-color-secondary); font-size:11px; }.material-source-line span { overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }.supplier-preview { display:flex; flex-wrap:wrap; gap:4px 8px; margin-top:6px; color:var(--el-text-color-secondary); font-size:11px; line-height:1.45; }.supplier-preview span { max-width:100%; }.supplier-preview em { color:var(--el-color-success); font-style:normal; }
.inline-empty { padding:18px; text-align:center; color:var(--el-text-color-secondary); background:var(--el-fill-color-light); border-radius:8px; }
.form-actions { display:flex; justify-content:flex-end; gap:10px; margin-top:20px; padding-top:16px; border-top:1px solid var(--el-border-color-lighter); }
.result-alert + .result-alert { margin-top:8px; }.sample-list { margin-top:14px; }.sample-item { display:grid; grid-template-columns:45px 1fr; gap:10px; padding:12px 0; border-bottom:1px solid var(--el-border-color-lighter); }.sample-index { font-family:ui-monospace, SFMono-Regular, Menlo, monospace; color:var(--el-color-primary); }.sample-item p { margin:5px 0; font-size:15px; }.sample-item small { color:var(--el-text-color-secondary); line-height:1.5; }.analysis-collapse { margin-top:12px; }.analysis-row { display:grid; gap:4px; margin-bottom:12px; }.analysis-row span { color:var(--el-text-color-secondary); font-size:12px; }.copy-area { margin-top:14px; }.copy-area :deep(textarea) { font-family:'Source Han Sans SC','PingFang SC',sans-serif; line-height:1.7; }.saved-hint { display:flex; justify-content:center; align-items:center; gap:6px; margin-top:12px; color:var(--el-color-success); }
.history-toolbar { display:flex; justify-content:space-between; gap:12px; margin-bottom:14px; }.history-toolbar .el-input { width:320px; }.history-card :deep(.el-table__row) { cursor:pointer; }
.detail-summary { display:grid; grid-template-columns:1fr 1.2fr; gap:12px; margin-bottom:18px; }.detail-summary div { padding:12px; background:var(--el-fill-color-light); border-radius:8px; }.detail-summary span, .detail-summary strong { display:block; }.detail-summary span { color:var(--el-text-color-secondary); font-size:12px; margin-bottom:5px; }.version-bar { display:flex; justify-content:space-between; gap:12px; margin-bottom:14px; }.version-bar .el-select { width:240px; }.detail-table { margin-top:14px; }.version-history { margin-top:24px; }.version-history h3 { font-size:16px; }.version-history p { margin:4px 0; }.version-history small { color:var(--el-text-color-secondary); }
.edit-table { margin-top:14px; }.edit-table :deep(.el-input-number) { width:100%; }.reason-form { margin-top:16px; }
@media (max-width:1180px) { .create-grid { grid-template-columns:1fr; }.result-panel { position:static; max-height:none; } }
@media (max-width:760px) { .page-intro, .panel-heading, .subheading, .version-bar, .scope-paste-heading, .normalized-scope-heading, .material-import-actions { flex-direction:column; align-items:stretch; }.scope-paste-heading .el-button, .normalized-scope-heading .el-button, .material-import-actions .el-button { width:100%; }.unit-result-heading { align-items:flex-start; flex-direction:column; }.application-fields, .detail-summary { grid-template-columns:1fr; }.range-head { display:none; }.range-row { grid-template-columns:1fr; }.material-row { grid-template-columns:1fr 1fr; }.material-row > :first-child, .material-row > :nth-child(2) { grid-column:span 2; }.history-toolbar { flex-direction:column; }.history-toolbar .el-input { width:100%; } }
</style>
