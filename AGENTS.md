<h1>AGENTS.md — トレードボット学習ログ</h1>

<p>振り返りから抽出したルールを蓄積します。<br>
振り返り全文は <code>/app/data/reflections/trade_{id}.md</code> に保存されています。</p>

<h2>基本ルール（手動設定）</h2>
<ul>
  <li>
    <strong>タイムパラメータ</strong>
    <ul>
      <li>最小保有時間: 2時間（EXIT禁止、<code>POSITION_MIN_HOURS</code>）</li>
      <li>最大保有時間: 4時間（強制決済、<code>POSITION_MAX_HOURS</code>）</li>
      <li>分析間隔: 15分ごと（<code>CYCLE_INTERVAL_MINUTES</code>）</li>
    </ul>
  </li>
  <li>
    <strong>ポジション設定</strong>
    <ul>
      <li>サイズ: $100 USD（ノーショナル＝実際に動くポジションサイズ）</li>
      <li>レバレッジ: 証拠金計算のみに影響（PnL = $100 × 価格変動%）</li>
    </ul>
  </li>
  <li>
    <strong>ルール管理</strong>
    <ul>
      <li>学習済みルールは最大10件を維持。超過時は統合または削除してから追加する。エントリー推奨条件は件数制限なし</li>
      <li>厳格な禁止ルールには <code>&lt;span class="rule-exception"&gt;例外: ...&lt;/span&gt;</code> と <code>&lt;span class="rule-source"&gt;出典: ...&lt;/span&gt;</code> を必ず記載。出典（Google Scholar / arXiv）が見つからない場合は警戒・注意の表現に留めること</li>
      <li>各 <code>&lt;li&gt;</code> の末尾に <code>&lt;small class="rule-stat"&gt;適用N / WINN&lt;/small&gt;</code> を付与すること</li>
    </ul>
  </li>
  <li>
    <strong>ルール見直し</strong>
    <ul>
      <li>振り返りごとに各セクションについて追加・更新・削除を検討すること</li>
      <li>適用5回以上かつWIN率40%以下のルールは改定または削除を検討すること</li>
      <li>市場レジーム変化（強気↔弱気転換）が確認された際は全ルールを能動的に見直すこと</li>
      <li>LOSSの原因はトレーダーではなくルールにある。敗因分析では「ルールのどこが悪かったか」を最優先で疑い、ルールの修正・削除を行うこと</li>
    </ul>
  </li>
</ul>

<h2>学習済みルール</h2>
<p>（振り返りから自動追加・更新・洗練された最大１０件のルール）</p>
<ol>
  <li><strong>マクロトレンド優先</strong>: 月足・週足・日足すべて下降時、ロング禁止。<span class="rule-exception">例外: 重大カタリスト当日確認済み、または月足陽線3本連続で上昇転換確定時。</span><span class="rule-source">出典: Moskowitz et al. (2012). "Time Series Momentum." JFE, 104(2), 228-250.</span> <small class="rule-stat">適用2 / WIN0</small></li>
  <li><strong>回復トレードの乗り遅れ防止</strong>: 急落回復60%超進行時、追いかけロング禁止。 <small class="rule-stat">適用1 / WIN0</small></li>
  <li><strong>短期足クロス一致</strong>: 5m DC中ロング禁止。5m GC+RSI≥40→ショート延期。上位TF DC＞下位TF GC。<span class="rule-exception">例外: 15m/30m/1h三重DC時、弱いGC（RSI≤55+上昇幅0.3%未満）は延期免除。</span><span class="rule-source">出典: Brock et al. (1992). Journal of Finance, 47(5), 1731-1764.</span> <small class="rule-stat">適用5 / WIN2</small></li>
  <li><strong>フィードラグ耐性</strong>: 往復フィー2倍以上の期待利益必要（最低$0.60）。 <small class="rule-stat">適用0 / WIN0</small></li>
  <li><strong>シグナル鮮度</strong>: 参照TFの直近2本以内のシグナルのみ有効。<span class="rule-exception">例外: 日足以上は直近5本まで許容。</span><span class="rule-source">出典: Lo et al. (2000). Journal of Finance, 55(4), 1705-1765.</span> <small class="rule-stat">適用1 / WIN0</small></li>
  <li><strong>ショート安全チェック</strong>: ①急騰高値1%超下落後フィー再検証 ②3h超コンソリ→ブルフラッグ→ショート禁止 ③1h/30m GC中は$200超下落余地確認 ④1h RSI≤20→ショート禁止。RSI 20-35は警戒のみ。<span class="rule-exception">例外: ②重大ネガティブカタリスト時は除く。</span><span class="rule-source">出典: Wilder (1978). / Bulkowski (2021). "Encyclopedia of Chart Patterns."</span> <small class="rule-stat">適用3 / WIN2</small></li>
  <li><strong>週足RSI過売れ時の利益目標圧縮</strong>: 週足RSI25-30でショート時、利益目標を次サポートの50-70%に縮小。 <small class="rule-stat">適用2 / WIN1</small></li>
</ol>

<h2>エントリー推奨条件</h2>
<p>（振り返りから自動追加・更新・洗練されたポジティブルール）</p>
<ol>
  <li><strong>マクロベア×DCBショート</strong>: マクロ三足下降＋日足SMA20レジスタンス、日足RSI30-49時に5m/15m RSI≥70→下落転換（&lt;65）確認→ショート。日足RSI≥50上昇中は禁止。<span class="rule-source">出典: Wilder (1978). / Chong &amp; Ng (2008). Applied Economics Letters, 15(14), 1111-1114.</span> <small class="rule-stat">適用0 / WIN0</small></li>
  <li><strong>5分足DC＋RSIサイクル確認ショート</strong>: マクロ三足下降＋日足SMA20レジスタンスで、5m DC中に5m RSI60→50以下転換→10分以内に執行。 <small class="rule-stat">適用0 / WIN0</small></li>
  <li><strong>ダブルGC＋週足過売れ回復ロング</strong>: 週足RSI25-35回復中、①30m非DC ②15m GC ③5m GC（10分以内）④1h GC維持→1hサポートでロング。 <small class="rule-stat">適用1 / WIN0</small></li>
  <li><strong>1h GC急落バウンスロング</strong>: 1h GC維持＋日中急落2%超＋1m/5m RSI同時≤25→平均回帰ロング。フィー再検証必須。<span class="rule-source">出典: Miwa (2018). SSRN 3174484. / Bremer &amp; Sweeney (1991). JF, 46(2), 747-754.</span> <small class="rule-stat">適用0 / WIN0</small></li>
  <li><strong>マルチTF DC収束ショート</strong>: マクロ三足下降＋15m/30m/1h DC収束でショート。5m: DC or 弱いGC（RSI≤55+上昇幅0.3%未満）。15m RSI 40-60、1h RSI≤55下降中。日足RSI≥50上昇中は禁止。<span class="rule-source">出典: Hill, A. SSRN 3412429. / Brock et al. (1992). JF, 47(5), 1731-1764.</span> <small class="rule-stat">適用4 / WIN2</small></li>
  <li><strong>ベアトレンド内バウンス天井ショート</strong>: マクロ三足下降＋15m/30m/1h全DC中、1m RSI≥70＋価格が15m SMA50 or 30m SMA20接触→ショート。1h RSI≤20時は禁止。<span class="rule-source">出典: Johannsen (2017). SSRN 2961304. / Moskowitz et al. (2012). JFE, 104(2), 228-250.</span> <small class="rule-stat">適用0 / WIN0</small></li>
</ol>
