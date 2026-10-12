# NewsWeave / WPuploader 機能分離の設計と実施計画

作成日・更新日: 2026-10-12（日本時間）  
状態: 独立アップローダーと生成側成果物出力を実装済み。稼働設定・既存投稿への適用は未実施。
実装済みの起動手順・初版の範囲は [実装手順](../../newsWeave/docs/independent-apps-implementation.md) を参照。
対象: V:\codex\newsWeave と V:\codex\WPuploader。依頼文の NewWeave は現行 News Weave を指すものとして扱う。

## 1. 採用案と独立動作の条件

記事生成は NewsWeave、WordPress の下書き登録は WPuploader が担当する。受け渡しは、本文・出典・検証記録・画像をまとめた変更しない ZIP ファイルとする。共有ディレクトリに DB や全件を書き換える管理 JSON を置かない。

NewsWeave は既存 PostgreSQL を生成用に継続利用する。WPuploader は実行ホストのローカル SQLite に受信・投稿履歴を保存する。軽量化の対象は受け渡しと投稿管理であり、生成側 DB 全体の置換は今回の範囲外とする。

~~~mermaid
flowchart LR
    N[NewsWeave: 収集・執筆・検証・画像生成] --> D[(生成専用 PostgreSQL)]
    N --> A[ローカル完成原稿・export 待ち]
    A --> Q[article-transfer/ready: 完成 ZIP]
    Q --> U[WPuploader: 検査・投稿・照合]
    U --> S[(ローカル SQLite)]
    U --> W[WordPress: 下書き・画像]
    U --> R[receipts: 任意の結果通知]
    R -. 表示用の任意取り込み .-> N
~~~

| 状況 | 動作 |
| --- | --- |
| WPuploader 未配置・停止 | NewsWeave は生成・検証・画像生成・ローカル保存まで完了する |
| WordPress 停止・認証失敗 | NewsWeave は通常動作。WPuploader 側が待機・再試行する |
| 共有フォルダー未設定・切断 | NewsWeave はローカル保存を続け、export だけ保留する |
| NewsWeave のコード・DB・API がない | WPuploader は持ち込まれた適合 ZIP だけで動作する |
| AI の設定・キーがない | WPuploader は動作する。AI を呼ばない |
| 結果通知未配信・未取り込み | 両アプリの本来の処理は継続し、表示だけ遅れる |

共通の実行パッケージ、相手の Python import、共有 DB、相手の API を導入しない。受け渡し仕様・JSON Schema・適合サンプルを双方に同梱する。WPuploader は通常の post/posts にも単独で登録できる。専用投稿タイプを使う際の WordPress プラグインは、NewsWeave アプリとは独立配布する。

## 2. 現行実装の調査と責務の移動

初回調査時点の WPuploader フォルダーには実装ファイルがない。NewsWeave は Python / FastAPI / SQLAlchemy / PostgreSQL / APScheduler を使用し、投稿処理が同じ DB・スケジューラーに組み込まれている。

| 現行ファイル・責務 | 分離方針 |
| --- | --- |
| app/orchestration/publication.py | 最新原稿・検証・レビュー確認は生成側の export 準備へ。送信・照合は WPuploader で独立実装する。旧 SQLAlchemy サービスをそのまま移さない |
| app/modules/publication/service.py | Markdown 変換、HTML 制限、出典整形は生成側 rendering へ。WP 分類 ID 解決、送信用 payload、slug、remote hash は uploader へ |
| app/infrastructure/wordpress.py / app/modules/publication/ports.py | client / port を uploader へ移植。NewsWeave の Settings、EditorialError、DTO import を取り除く。HTTPS・接続先検査・不明送信の安易な再実行禁止は保持 |
| app/jobs/scheduler.py | process_verified_publications、reconcile_wordpress、decorate_wordpress_drafts を除去。生成用画像準備と export 待ちの再処理へ置換 |
| app/orchestration/featured_images.py | 画像生成・JPEG 検査・実モデル記録は生成側に残して draft に紐づける。media 登録・アイキャッチ添付・照合は uploader へ |
| app/orchestration/model_disclosure.py / app/modules/publication/model_disclosure.py | 実行記録の解決と本文表示は生成側で完成させる。FeaturedImage の投稿依存を除去。uploader でモデル名を推測しない |
| app/api/routes/publications.py / app/main.py | WP 操作ルーターを生成側から外す。export 一覧、ローカル preview、任意の receipt 表示に置換 |
| app/orchestration/editorial.py | 改稿条件の Publication 照会、公開・投稿履歴への依存を除去。生成側の検証・レビュー・改稿規則で判断する |
| app/orchestration/state_machine.py / DB models | 原稿と送信の状態を分ける。WP_DRAFTED / PUBLISH_APPROVED / PUBLISHED を生成の進行条件にしない。旧値は移行履歴として残す |
| app/contracts/editorial_v1.py | wordpress_preview_checked を生成側ローカル preview の確認へ変更。新版契約と移行 adapter を用意し、WP 存在をレビューの必須条件にしない |
| Publication / FeaturedImage / PublishApproval / WP scope の SourceSetClaim | 投稿・media・公開履歴・投稿先の出典予約は uploader へ。writing scope の生成重複予約は生成側に残す |
| app/orchestration/wordpress_routes.py / tools/migrate_wordpress_routes.py | 旧投稿タイプの route 移行機能は廃止し、uploader に移植しない。現行投稿先の履歴取り込みは別の独立ツールで行う |
| wordpress/ / tools/build_wordpress_plugin.py / WP 専用テスト | 現行専用投稿タイプだけを uploader 側で独立配布。旧投稿タイプの互換分岐・移行画面・移行実装・互換テストは削除対象 |
| .env.example / compose.yaml / README.md / docs/setup-and-wordpress.md | WP 認証・分類 ID map を uploader 側だけに配置。ビルド、設定、起動、ネットワークを独立させる |

WPuploader v1 の通常機能は下書き登録・画像添付・読み取り照合とし、作成時の status は draft に固定する。旧公開承認・公開・削除 API は停止して履歴を保存し、公開は WordPress 管理画面で行う。将来 API を復活させる場合は uploader の別機能として明示承認と競合確認を実装する。

## 3. ディレクトリと保存先

配置例。今回、この受け渡しディレクトリは作成していない。

~~~text
V:\codex\
  newsWeave\                         # 生成プロジェクト
  WPuploader\                        # 独立 Python プロジェクト
    wpuploader\                      # app という共通名を避ける
    contracts\                       # Schema、正常・異常サンプル
    tests\
    docs\
    pyproject.toml / lockfile / Dockerfile / compose.yaml / .env.example
  article-transfer\                  # 任意。両プロジェクトの外
    staging\                         # 生成側: 書きかけ。reader は無視
    ready\
      <bundle_sha256>.nwdraft        # 生成側: 完成 ZIP
    receipts\
      <target_id>\<event_id>.json     # uploader: 完成結果通知
    archive\                         # 確認後の手動保管用
~~~

- 今回 V: は UNC \\TRUENAS\dev 上のフォルダーに解決される。コード・ZIP は NAS に置けるが、SQLite / journal / worker lock は NAS に置かない。
- Windows の状態保存例は %LOCALAPPDATA%\WPuploader\<instance_id>\state.sqlite3、cache\、worker.lock。Docker は実体がローカルディスクの永続 volume または bind mount。SMB/NFS volume は使わない。
- NewsWeave は自分の DB と永続 artifact 領域に完成原稿・export 待ちを保存し、共有側の消失から再出力できるようにする。生成コンテナーに永続保存先を追加する。
- staging/ready の書き手は NewsWeave、読者は WPuploader。receipts は逆。相手のファイルを編集・削除しない。
- 処理後も ready を移動・削除しない。初版は自動削除を設けず、backup と全投稿先の処理確認後に保守操作で保管・削除する。
- 共有フォルダーを使わず、生成側 export --output と uploader import <file> による手動コピーも可能とする。

SQLite WAL は同一ホストの共有メモリーを前提としてネットワークファイルシステムでは動作しない。共有するのを完成ファイルだけにする理由である。[SQLite 公式 WAL 説明](https://www.sqlite.org/wal.html)

## 4. 受け渡し契約: DraftBundle v1

### 4.1 ZIP の内容と本文

~~~text
package.json                 # metadata と file manifest
article.md                   # 完成原稿。確認用
article.html                 # 投稿に使う完成 HTML
assets/featured.jpg          # 画像有効時だけ存在
~~~

拡張子は .nwdraft、中身は通常の ZIP。WPuploader は article.html を使い、Markdown を再レンダリングしない。NewsWeave が比較表、出典、実際に使用した AI モデル表示まで完成させる。uploader は同じ版の HTML allowlist への適合を確認し、不適合なら本文を勝手に修正して送らず保留する。画像は本文に追加せず featured_media として扱う。

WordPress の数値分類 ID、remote media ID、認証情報、相手 DB を参照するための接続先を bundle に含めない。他の producer も契約に沿って作成できる。

### 4.2 metadata の必須項目

| 項目 | 意味・制約 |
| --- | --- |
| schema_version | 1.0。初版は完全一致で受理。将来互換版は reader 許可一覧と適合試験で追加 |
| producer_id | 環境を識別する永続 UUID。再起動や更新で変えない。別環境は別 UUID |
| package_id | 論理キーから UUIDv5 で生成。再 export で変えない |
| article_id / draft_id / revision | 原稿の UUID・本文改訂番号。受信側 DB の外部キーにはしない |
| artifact_revision | 画像や整形等、本文 revision 以外の成果物改訂番号。1 から開始 |
| created_at / producer_version | snapshot の固定 UTC 日時・生成版。再配信時に現在時刻へ変えない |
| title / excerpt | 投稿タイトル・要約。lead を本文へ重複挿入しない |
| category_keys / tag_keys | 論理分類キー。WP 数値 ID は uploader 設定で解決 |
| source_urls / source_set_hash / source_normalization_version | 正規化 URL 集合と hash・規則版。reader でも hash を再計算 |
| eligibility | 検証 pass、対象 draft/package hash、完成 HTML hash と render_binding、policy version、実行モード、review 要否・結果・対象 hash。shadow 原稿は禁止 |
| provenance | 記事の各工程の実モデル・関連記録 ID。収集元本文全文・秘密情報は含めない |
| featured_image | 有効なら path、mime、寸法、alt、caption、実モデル。無効なら null と disabled/manual_skip の理由 |
| files | package.json 以外の全 file の相対 path、byte size、SHA-256 |
| manifest_hash | この項目だけ除いた package.json の canonical JSON の SHA-256 |

canonical JSON は UTF-8、BOM なし、キー昇順、ensure_ascii=False、区切り (',', ':') とする。UUID は小文字標準表記、日時は UTC Z、数値は整数のみ。NaN/float、JSON の重複キーを拒否する。本文の改行は LF に統一した完成 bytes を hash する。files の本文・画像 hash を eligibility の対象とも結び付ける。

ZIP 全体の hash は外側のファイル名と受信 DB に保存し、ZIP 内に循環参照させない。論理キーは canonical JSON 配列 [producer_id, article_id, revision, artifact_revision]。UUIDv5 の namespace は契約仕様で固定し、実装開始時に双方へ同梱する。同じ論理キーに異なる manifest_hash が届いたら PACKAGE_ID_CONFLICT とし、先着採用・上書きしない。

初版の受理上限案は ZIP 25 MiB、展開合計 40 MiB、metadata 256 KiB、HTML 200,000 文字、title 500 文字、excerpt 2,000 文字、file 数 8。現行出力契約の制約も export 時に検査する。ZIP の絶対 path、..、symlink、重複 entry、未記載 file、過大な圧縮比を拒否する。原則として展開せず許可 entry を読んで検査する。

### 4.3 出典・承認・信頼境界

既存 app/core/source_set.py と app/core/content.py の URL 正規化を source-set-v1 として凍結し、同じ URL→hash の fixture を双方に同梱する。単なる URL ソートへ置き換えて既存重複防止を変えない。

NewsWeave は export snapshot 作成時に最新原稿・対象検証・レビュー条件を確認する。REVIEW_REQUIRED=true なら人間レビューと必要な複数人確認を満たす。WPuploader は snapshot の形式・hash・eligibility と自分の運用設定を確認し、生成側ユーザー DB を参照しない。uploader 側の review 必須設定は producer の false で緩和できない。

hash は破損検出であり、承認者の真正性を証明しない。初版の入力は書き込み権限を制限した共有フォルダーまたは明示 import する信頼済みファイル。第三者から自動受信する際には別途署名検証を追加する。

### 4.4 画像を生成側で完成させる

現行画像処理は WordPress Publication の後に始まるため順序を変更する。生成側が draft_id・本文 hash に紐づけて画像生成、JPEG 検査、実モデル記録を終え、bundle を確定する。uploader は完成画像を登録するだけとし、AI キーを持たない。

画像失敗は対象原稿だけ asset_pending にし、本文・検証を保存して他の記事を続ける。既存画像再利用・再生成回数制限・明示 retry を用意し、無制限の有料再生成をしない。画像有効時の欠損を勝手に無画像扱いにしない。画像なしは設定または明示 manual_skip を記録する。完成後の画像追加は artifact_revision を上げた別 bundle とする。

## 5. NewsWeave の生成・export 手順

1. 最新 draft と対象検証・必要レビューが一致することを確認する。出典数、writing scope の重複防止、shadow 禁止を維持する。
2. CMS 非依存の rendering で完成原稿を作り、画像・実モデル・approval とともに export snapshot / 待ち行を生成側 DB へ保存する。
3. ZIP を永続 artifact 領域に一時保存、再読込検証後に完成 artifact を記録する。ZIP entry 順、日時、圧縮方式を固定し、同じ snapshot から同じ bytes を出す。中断しても snapshot から再生成できる。
4. 共有側 staging/<worker_uuid>/<bundle_hash>.part にコピーし、flush / close、再読込検証する。その後同じ共有ファイルシステム内の ready/<bundle_hash>.nwdraft へ公開する。既存完成 file は同じ hash なら成功、異なるなら競合とする。
5. rename の応答消失は ready の再検査で確かめる。公開後に export 配信済みを記録する。DB 記録が遅れても同じ artifact と照合して収束する。
6. 未配信を再処理し、配信済みも巡回して file 欠損なら保存済み artifact から再出力する。共有切断・ディスク不足は export だけを保留する。

DB と file を一つの transaction に見せない。snapshot が原稿内容の正本、artifact が配信 bytes の正本であり、繰り返し照合で一致させる。

原稿完成、共有配信、WordPress 登録を別状態にする。draft_ready、export pending/delivered、任意の投稿結果表示を分け、receipt を待って生成・改稿を止めない。

NAS の rename が完全に原子的・永続的であるとは無条件に仮定しない。reader は ZIP と全 hash を検査して不完全な file を送らない。一時読込失敗は次回再検査し、実環境 SMB/TrueNAS で切断と同時公開を試験する。

## 6. WPuploader の管理データと排他

### 6.1 SQLite の最小構成

標準 sqlite3 を使用し、PostgreSQL、Redis、broker を追加しない。外部依存は HTTP client、契約検証、HTML/画像検査、CLI に限定する。

| テーブル | 一意性・主要項目 |
| --- | --- |
| bundles | package_id UNIQUE、論理キー UNIQUE、manifest/bundle hash、cache path、metadata、検査状態 |
| deliveries | (target_id, package_id) UNIQUE、operation key、固定 payload/hash、state、remote post ID/hash/status、attempt ID、次回日時、error |
| source_claims | (target_id, source_set_hash) UNIQUE、owner producer/article、採用 package ID、remote ID。失敗・不明でも自動解放しない |
| media_deliveries | (target_id, package_id, asset_hash) UNIQUE、media slug、ID、upload/attach 個別状態 |
| events | event_id UNIQUE、attempt、状態遷移、通知 JSON、receipt 配信済み flag |
| meta | schema version、instance ID、target 設定 fingerprint、移行情報 |

実装は foreign_keys=ON、journal_mode=DELETE、synchronous=FULL、busy_timeout=5000 を使用する。古い標準 sqlite3 runtime の WAL reset 不具合を避けるため、初版は rollback journal を採用した。remote 操作は OS lock で直列化し、短い transaction を使用する。[SQLite journal の仕様](https://www.sqlite.org/lockingv3.html)

取り込み・予約は短い BEGIN IMMEDIATE と UNIQUE 制約で行う。HTTP 中に DB transaction を保持しない。busy は jitter 付き有限回 retry、解消しなければその巡回を一時終了して次回へ。schema migration は worker 停止・backup・排他取得後に実行する。

### 6.2 同時実行の保証範囲

初版は「NewsWeave と WPuploader の同時動作」と「同一ホスト・同一状態ディレクトリの複数 uploader プロセス」に対応する。同一 target への別ホスト・独立 DB の active-active は対象外。

- remote 操作をする全 CLI・常駐処理は同じローカル worker.lock の OS 排他ロックを取得する。Windows は byte-range lock、Linux は flock 等。プロセス終了で OS が解放する。file の有無や時刻による stale 判定を使わない。
- lock は remote 操作と結果 commit まで保持する。別 worker は送信せず一時終了する。初版は remote 操作を一つずつ行い、lease 期限による takeover を導入しない。
- producer はこの lock を使わない。取り込み・読み取りは SQLite の短い transaction で進める。
- 一つの target の active uploader は一つのホスト・状態ディレクトリに固定する。Docker の複数実行も同じローカル volume を使う。ホスト移行は旧 worker 停止を確認してから。
- 別ホストの自動排他が必要なら WordPress 側の一意 operation key API 等を追加する。NAS 上の期限付き JSON lock だけで重複送信を防げるとはしない。

### 6.3 取り込みと状態遷移

ZIP をローカル cache にコピーし、完成 bytes を検査してから bundles に登録する。共有 file は変更しない。同じ論理キー・hash の再取り込みは成功、異なる hash は conflict。共有切断後も検査済み cache は送信できる。

~~~text
received -> validated -> prepared -> creating -> draft_created
                                               |
                画像なし ----------------------> completed
                画像あり -> media_prepared -> media_creating
                         -> media_created -> attaching -> completed

creating / media_creating / attaching -> uncertain -> GET 照合 -> 続行
検査・分類不足 -> blocked   修正後に再検査
remote 改変・候補重複 -> conflict   個別確認
~~~

prepared は route、分類 ID、本文、slug、payload hash が固定された状態。remote write 前に creating と attempt ID を commit する。再実行時に設定の変更で payload を作り直さない。未送信の分類不足等の設定修正だけ、明示 prepare のやり直しを許可する。

## 7. WordPress の冪等性と復旧

### 7.1 識別と重複防止

target_id は正規化 base URL、post type、REST base から既存 wordpress_target と互換の規則で作る。対応対象は post/posts と news_weave/news-weave。旧投稿タイプの互換・移行は今後のバージョンで実装しない。現在サポートする投稿先の URL/route を変える場合も、状態を引き継がずに全件新規 POST しない。

新 operation key は canonical JSON 配列 [target_id, producer_id, article_id, revision, artifact_revision] の SHA-256。slug は news-<key>。media は operation key と asset hash から識別する。移行する旧行は旧 key・slug をそのまま保存する。

WordPress は draft/pending の slug 一意性を検査しない。slug は照合の手掛かりであり remote 排他・exactly-once の保証ではない。[WordPress wp_unique_post_slug の仕様・実装](https://developer.wordpress.org/reference/functions/wp_unique_post_slug/)

### 7.2 標準 REST API を使う初版

1. 契約検査、分類解決、source claim 予約を済ませ、まず対象 route、slug、context=edit で既存投稿を照会する。
2. 候補一件なら ID、type、slug、raw title/content/excerpt、分類、アイキャッチ、status を固定 payload と比較し、一致した draft を採用する。複数・不一致・公開済み・trash は conflict とし新規作成しない。
3. 未送信 prepared、既存候補なし、worker lock 保持の場合だけ creating を commit して一度 POST する。作成 status は draft。
4. 成功応答の ID・raw 内容を検査して draft_created を commit。画像があれば次へ。
5. timeout、connection reset、5xx、応答破損、送信中 crash は uncertain 相当。次回は GET 照合し、投稿が見つかれば復旧する。見つからなくても新規 POST を自動反復しない。
6. 照合では draft のほか pending/private/publish/future、可能な範囲で trash を確認する。権限や API の制約で見えない投稿を「存在しない」と断定しない。既存 client の find は trash を除外するため移植時に補う。

投稿と照会条件は [WordPress Posts API](https://developer.wordpress.org/rest-api/reference/posts/) に従う。専用投稿タイプは実環境で route、taxonomies、raw 取得可否を確認する。

creating commit 後・POST 前の crash と、POST 成功後・応答前の crash は client だけでは完全に区別できない。前者は remote に何もなく自動復旧しない場合がある。対象記事だけ保留し、他の記事は進める。明示復旧で既存 post ID を採用するか、remote 処理終了と未作成を確認して再送する。時間経過や GET に出ないことだけを未作成の証拠にしない。

したがって通常の反復実行・同一ホスト競合・応答消失後に投稿が見つかるケースは自動修復するが、すべての不明送信を無人で解消する保証は初版に含めない。

### 7.3 media と手動編集

media 登録も media_creating を先に commit。不明結果で同じ画像を盲目的に POST しない。候補 ID、slug、mime、寸法、可能なら認証付き取得 bytes の hash を検査する。名前と寸法だけでは一致と断定せず、確認不能なら画像工程のみ保留して本文下書きの成功を記録する。

アイキャッチ設定は既知 post ID を更新する。直前に status・remote hash を確認し、必要な featured_media だけ送る。モデル表示は完成 bundle に含まれるため本文更新は不要。結果不明なら GET で目的 media ID が付いたか照合する。

再実行で人間が編集・公開した記事を上書きしたり draft に戻したりしない。標準 REST の read→write 間の手動編集は原子的には防げないため、処理中の手動編集を避け、検出した改変は conflict とする。厳密な compare-and-swap が必要ならサーバー側機能を追加する。

### 7.4 出典集合と改稿

現在の「同じ出典 URL 集合から別の記事を自動生成・投稿しない」規則を保持する。生成側 writing 予約とは別に uploader は target ごとの source_claims を持ち、別 producer/article の同じ source_set_hash を自動送信しない。不明・失敗操作の予約を自動削除しない。

同じ原稿の再 export は無操作。未送信の複数版では取り込み済みの最大 revision / artifact_revision を採用し、古い未送信版を superseded にする。まだ届いていない将来版は予測しない。

既に draft_created/uncertain の版がある場合、新版を自動で別投稿にしない。revision_pending とし、旧 draft の未編集・未公開を確かめる明示 replace だけ許可する。遅れて届いた古い版で巻き戻さない。生成側の改稿は uploader・receipt 不在でも可能とする。

### 7.5 将来の完全自動復旧

別ホスト active-active や不明送信の完全自動 retry が必要なら、WordPress 側に (target, operation_key) UNIQUE、固定 payload hash、既存結果を返す専用 endpoint を設計する。同じ key・異なる hash は 409 とする。

post meta に key を付けるだけでは投稿と記録の原子性がない。予約・投稿・結果記録の crash 窓、WordPress hooks の副作用、DB engine、rollback を検証する必要がある。軽量な初版では必須にせず、必要性が確認された後の別工程とする。

## 8. 結果通知・故障時の収束・backup

receipt は任意通知。投稿管理の正本は uploader SQLite とする。schema version、event ID、instance ID、target ID、package ID、manifest hash、state、remote IDs/URL、error code、UTC 日時、target/package 内の単調増加 sequence を含め、本文・秘密情報を含めない。

通知待ち event は結果と同じ DB transaction で保存し、一時 file から完成 JSON を公開する。失敗しても次回同じ event を配信する。生成側は event ID で重複排除し、古い sequence・別 target の結果で表示を巻き戻さない。対応原稿がない通知は保留または無視し、生成を止めない。receipt により原稿の適格性を変更しない。

| 障害 | 回復・扱い |
| --- | --- |
| ZIP 作成・コピー中に停止 | .part は無視。保存 snapshot から再 export |
| ready 公開後・配信済み DB 記録前に停止 | 同じ hash の ready を確認して記録 |
| NAS が一時的に途中状態を見せる | ZIP/hash 検査で未受理。次回再検査 |
| 同じ bundle を反復走査 | UNIQUE と hash で一件に収束 |
| 同じ package ID に異なる内容 | conflict。原本を変更せず新しい正しい版を要求 |
| cache 保存後・DB commit 前に停止 | 再取り込み。同じ cache hash は再利用 |
| 複数 uploader 同時起動 | OS lock を取得した一つだけ remote 操作 |
| SQLite busy | 有限 retry。未処理を残して次回へ |
| 投稿成功後・ローカル commit 前に停止 | creating から GET 照合、同じ ID/hash なら復旧 |
| creating 保存後・POST 前に停止 | 個別 uncertain。結果がなければ明示復旧が必要 |
| 結果保存後・receipt 配信前に切断 | 保存 event を後で配信し、投稿は繰り返さない |
| 分類不足・schema 未対応・恒常破損 | bundle 単位で blocked。更新・再 export 後に再検査 |
| 手動編集・公開・削除 | conflict/remote_changed。自動復元しない |
| 生成側停止・ready 消失 | 検査済み cache があれば継続。なければ再 export を待つ |

watcher は補助とし、周期的全件走査を必須にする。cursor や更新日時だけで永久に落とさない。大量 file は段階的全件巡回を行う。blocked/uncertain が先頭を占有しないよう次回日時と一巡処理上限を設ける。

通常の SQLite crash は rollback journal recovery で回復する。物理破損・状態ディスク喪失は反復起動だけでは直らないため、SQLite backup API による整合 backup と cache/events を保持する。稼働 DB 単体の生コピーは行わない。

既存 instance の DB が消失しても空 DB を自動作成して全件 POST しない。backup 復元または明示 recover を使い、復元行を含む書き込み候補を照合専用で再確認する。backup が prepared まで巻き戻っている可能性も考慮する。receipt 不在を未送信の証拠にしない。新規導入は init --new-install 後に取り込みを始める。

## 9. 設定・CLI・デプロイ案

以下は新設予定であり、現在実行できるコマンドではない。

| アプリ | 設定・操作 |
| --- | --- |
| NewsWeave | AI、収集、比較、review、画像、生成 DB の設定を残す。DRAFT_ARTIFACT_DIR、任意 DRAFT_TRANSFER_DIR、DRAFT_EXPORT_ENABLED、永続 DRAFT_PRODUCER_ID を追加。不足資料収集は FETCH_MISSING_SOURCES=false を既定とする |
| NewsWeave CLI | drafts export <draft_id> --output <file>、drafts export-pending、drafts preview <draft_id> |
| WPuploader | WPUPLOADER_STATE_DIR、任意 WPUPLOADER_TRANSFER_DIR、WORDPRESS_* 接続・認証・分類 map、review 強制設定 |
| WPuploader CLI | init --new-install、import <file>、run-once、watch、status、reconcile、resolve、replace、backup、recover |
| 明示復旧 | resolve <key> --adopt-post-id <id>、resolve <key> --confirmed-not-created、replace <package_id> --expected-remote-hash <hash>。対象・照合結果を表示し監査記録を残す |
| dry-run | bundle 検査、payload preview、読み取り照合まで。post/media write はゼロ |

両者に独立 pyproject、lockfile、Dockerfile、compose を置く。相手の build context、DB、ネットワークを要求しない。uploader は ready を read-only mount、receipts だけ write mount、state をローカル永続保存する。

不足資料収集は fetch_missing_sources（環境変数 FETCH_MISSING_SOURCES）で切り替え、既定 false。無効時は手元資料だけで処理し、不足は needs_input と理由を返す。外部検索・補完取得・再取得を開始せず、通常の定期収集や明示通常収集は別に扱う。Request の true で実行側の無効を解除しない。詳細は記事生成モジュール設計第5節を参照する。

生成側 readiness は自分の DB/artifact 領域で判定し、共有の配信遅延は別 status。秘密情報は環境変数等から読み、bundle/receipt/log に出さない。

## 10. 実装・移行計画

### Phase 1: 契約固定と独立骨格

記事生成モジュール設計の G1 と合わせて、DraftBundle v1 Schema、canonicalization、固定 UUID namespace、source-set-v1、正常/破損/非適格/画像なし fixture、WPuploader の独立構成を作る。投稿先ごとの状態保存先と単一 active ホストを決める。

完了条件: 一方の Python package を外しても双方の契約試験が動作し、現行本文・出典・分類・モデル・画像を欠落なく表現できる。

### Phase 2: 生成側の完成原稿・画像・export

記事生成モジュール設計の G2・G3 で執筆・検証・画像・組み立てを独立化し、CMS 非依存 rendering、draft 単位の画像 asset、export snapshot/outbox、ローカル preview を追加する。現行経路を保持して出力を比較し、検証・review・出典数・重複予約・shadow 禁止を export 適格性へ移す。

Publication 依存の改稿条件と review 項目を更新する。旧 FeaturedImage bytes/実行記録を draft asset に移し、既存画像の再生成・再課金を避ける。

完了条件: WP 無効・WPuploader 未配置で生成、画像、preview、ローカル export が完了する。

### Phase 3: 受け渡しと独立 uploader

一時 ZIP→検査→公開、周期全件走査、cache、SQLite、OS lock、分類解決、固定 payload、下書き作成・照合、media/添付、receipt を実装する。旧 client を移植して NewsWeave import を除去する。

隔離 WordPress で通常 post と現行専用タイプを確認する。旧投稿タイプの互換・route 移行の実装や試験は行わない。

完了条件: NewsWeave コード・DB・AI キーを与えず fixture 原稿を登録できる。

### Phase 4: 履歴移行・dry-run

1. 旧投稿・media・照合 worker を停止し、remote 操作終了を確認する。生成は継続できる。
2. 旧 DB と WordPress を backup。Publication、FeaturedImage、PublishApproval、WP scope の SourceSetClaim、対象原稿、payload/remote hash、現行投稿先の識別情報を整合 snapshot として export。
3. 対応対象の現行投稿先の履歴を uploader へ取り込み、target、既存 key/slug、post/media ID、state、payload、出典予約を保持する。旧 FK は参照 ID と snapshot に置換し、生成側ユーザー認証 DB はコピーしない。
4. drafted/attached は GET 照合後に completed。creating/uploading/attaching は照合専用。published/trashing/trashed/legacy も再投稿抑止情報として保持する。
5. 現行投稿先の slug・CMS URL・ID を維持し、投稿タイプや route を変える処理をこの移行に含めない。非対応の旧投稿タイプの履歴を新規送信候補に変換しない。
6. dry-run で件数、payload hash、remote ID、全 source claims、画像、公開/削除抑止を照合する。不明履歴を prepared の新規送信にしない。

完了条件: 移行済み記事を繰り返し取り込んでも post/media が一件も増えない。

### Phase 5: 送信経路の切り替え

NewsWeave の WP 設定・投稿/添付ジョブ・操作 API を無効化してから export と uploader を有効化する。同一 target で旧 sender と新 sender を併走させない。少数原稿で全工程を確認後に範囲を広げる。

生成側の通常処理が WP 状態を参照しないこと、uploader が生成側を参照しないことを確認する。専用 WP plugin/build tool を uploader に移して独立配布する。

### Phase 6: 整理と運用文書

生成側から WP client、投稿サービス、設定、router import を除去し、残す rendering を整理。旧テーブルはすぐ drop せず読み取り専用履歴として一定期間残し、削除は別 migration。

旧投稿タイプの互換設定・分岐、wordpress/news-weave/includes/route-migration.php、移行画面、移行ツール、専用テストを将来版から除去する。比較選定のモード名 legacy は別機能なので、この削除対象に含めない。

双方の README、.env.example、compose、依存、テストを更新する。受け渡し契約、復旧、backup、保管、ホスト移行、旧公開 API 停止と管理画面での公開手順を文書化する。

### ロールバック

uploader の remote 操作を停止し、ready/state を保存したまま生成側のローカル生成を継続する。

旧 sender を再利用する際は、uploader が追加・変更した post/media ID、payload/remote hash、source claims を旧 DB へ逆移行・照合してから有効化する。旧コードだけ戻して送信しない。不明操作は照合・保留を先に行う。

## 11. 受入試験

| 試験 | 合格条件 |
| --- | --- |
| WP 認証・uploader を撤去し生成側だけ配置 | 生成・検証・画像・preview・ローカル export が成功 |
| 生成側コード・DB・AI を撤去し uploader だけ配置 | 持ち込んだ ZIP を下書き登録 |
| 生成と uploader の並行動作・連続公開 | 未完成 file を送らず完成原稿を全件取り込む |
| 同一ホストで複数 uploader | post/media が増えず一つだけ remote 操作 |
| 反復 import、cache/DB commit 前後で強制終了 | 一件に収束し履歴が壊れない |
| rename 前後の切断・共有欠損 | 生成を継続し共有復旧・巡回で配信回復 |
| remote 作成済みなのに timeout | POST を反復せず同じ ID へ GET 照合で復旧 |
| creating commit 後・POST 前で終了 | 個別 uncertain、他原稿は送信 |
| media 成功後・結果記録前で終了 | 無条件再登録せず照合または画像だけ保留 |
| 人間が編集・公開・trash 移動 | 上書き・再作成・draft 復帰しない |
| 同じ出典集合を別 article/producer で送る | target の予約で重複を止める |
| 改稿・画像追加・古い版の遅延到着 | 自動二重投稿・巻き戻しがない |
| receipt 重複・逆順・未配信 | 表示が巻き戻らず後で回復、本来処理は継続 |
| schema 未対応、分類不足、hash 不一致、ZIP traversal | 対象だけ blocked、正常記事は継続、対象 remote write はゼロ |
| 現行投稿先の履歴移行後の全件再走査 | 既存件数・ID・URL が変わらない |
| 新版の投稿タイプ対応 | 通常 post と現行専用タイプだけを扱い、旧投稿タイプ互換・route 移行を含まない |
| backup 復元・状態ディスク喪失 | 全件 POST を自動再開せず照合後に復旧 |
| 各プロジェクトを別 build context で build/test | 相手のフォルダー・パッケージを要求しない |
| 不足資料収集の設定未指定・無効 | 補完用の外部検索/取得/更新・取得 queue 作成がなく、対象だけ needs_input |
| 不足資料収集の明示有効化・途中無効化 | 有効時だけ上限内で補完し、無効化後は新しい取得を開始しない |

通常の検査に加え、DB commit と remote write の間、remote 成功とローカル結果保存の間を故障注入で検証する。SMB と OS lock は mock だけで完了とせず、使用する Windows/Docker ホストと TrueNAS で確認する。

## 12. 保証範囲と残る限界

- 起動、設定、DB、処理を独立させ、片方がなくても本来の機能を実行する。
- 受け渡しは immutable file、投稿管理はローカル SQLite。共有管理 JSON の更新競合と NAS 上の SQLite を避ける。
- 生成と投稿の同時実行、同一ホスト uploader 競合、反復受信、通常 crash は再実行・照合で回復する。
- 破損原稿・設定不足・不明送信を個別保留し、全体を実行不可能にしない。
- 標準 API の不明送信、別ホスト active-active、手動編集との原子的競合防止には限界がある。初版は個別保留・明示復旧とし、必要なら WordPress 側の専用 API を追加する。

実装は Phase 1 から開始する。旧 sender の停止と現行投稿先の履歴移行が完了するまでは、本番の新しい送信経路を有効にしない。

## 13. 記事生成モジュールの独立化

詳細は [記事生成モジュールの独立化設計・実施計画](../../newsWeave/docs/article-generation-module-plan-20261012.md) を参照する。

生成側は collect（収集）、ground（根拠整理）、curate（選定・比較準備）、write（執筆）、verify（検証）、illustrate（画像生成）、assemble（完成原稿組み立て）の七つに分ける。前工程の成果物を外部から与えれば、他モジュール・NewsWeave DB/API・WordPress がなくても単独実行できる。人間レビューは必要な運用だけ ApprovalRecord として入力する。

公開操作は run 一つとし、共通 Request/Result と自己完結 .nwstage（ZIP）で接続する。入力は role ごとの file/hash、出力は成果物・結果・不足理由。DB ID を暗黙に解決せず、必要な facts・原文・モデル記録を明示的に渡す。assemble が完成 .nwdraft を作り、WPuploader の入力を複雑にしない。

不足資料の追加収集は既定で無効とし、手元資料が十分なら続行、不足なら理由・checkpoint を保存して停止する。明示的な有効化時だけ不足補完ループを動かし、無効化時は保存済み queue も新しい取得を開始しない。G1 で共通方針と既定値を固定し、G4・G5 で全不足補完経路の判定と試験を実装する。

G1（契約）、G2（執筆・検証）、G3（画像・組み立て）をこの計画の Phase 1〜3 と連動して実施する。WPuploader の独立化を完了後、G4（収集・根拠整理）、G5（比較準備）、G6（薄い runner への切り替え）で上流まで独立化する。上流を旧 DB adapter で接続する移行期間は認めるが、最終的には各工程の独立 wheel/CLI・外部 DB 不要の適合試験を完了条件とする。

