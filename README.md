# WPuploader

NewsWeave のソース・DB・AI キーを要求しない WordPress 下書きアップローダー。
入力は完成した .nwdraft（ZIP）。投稿 status は draft に固定し、自動公開しない。
対応投稿タイプは post/posts と news_weave/news-weave。

## 起動

Python 3.12 以上で pip install .。環境変数を .env.example の項目に合わせて設定する。
Python CLI は環境変数を読み、.env を暗黙には読み込まない。Docker compose は .env を読む。

1. wpuploader --state-dir <ローカル保存先> init --new-install
2. wpuploader --state-dir <ローカル保存先> import <原稿.nwdraft>
3. wpuploader --state-dir <ローカル保存先> dry-run
4. 接続・分類 ID を確認して WORDPRESS_ENABLED=true を設定し run-once または watch。

共有時は WPUPLOADER_TRANSFER_DIR/ready/ を周期走査する。生成側の停止中も検査済み cache から送信できる。
SQLite/ロック/cache はローカル。NAS は完成原稿と任意 receipts だけに使う。
一つの投稿先の active uploader は一つのホスト・状態ディレクトリに固定する。

## 再実行・復旧

同一原稿は重複登録しない。送信前に意図を記録し、応答不明時は GET 照合する。
照合できない操作はその原稿だけ uncertain に保留し、他原稿を処理する。
resolve <package_id> --adopt-post-id <id> で一致する既存下書きを採用できる。
未作成を運用者が確認した場合だけ --confirmed-not-created で新規送信を再許可する。

人間が編集・公開・削除した記事を自動上書きしない。同じ資料集合の別記事を抑止する。
新 revision は明示置換が必要な状態として保持し、別の下書きを自動作成しない。

backup <file> は SQLite backup API による整合 backup。
DB 喪失時は空 DB を自動初期化しない。backup 復元後は照合を行う。
recover --confirm-reconciliation-only は共有原稿を照合専用として取り込み、remote write を行わない。

## テスト

pip install ".[dev]" の後に pytest。テストは fake CMS を使い外部へ投稿しない。
専用 WordPress plugin は wordpress/news-weave にあり、管理画面から独立配置できる。
設計詳細は docs/newsweave-separation-plan-20261012.md を参照。

Docker は共有なしで起動できる。最初は docker compose run --rm uploader init --new-install で台帳を用意する。
共有する場合だけ compose.transfer.yaml を重ね、DRAFT_TRANSFER_HOST を指定する。
既存の送信履歴がある場合は、空台帳で投稿を始める前に migrate-history と reconcile を行う。
既存台帳の復元後も recover --confirm-reconciliation-only を使って不明試行を照合専用に戻す。

置換は replace <package_id> --expected-remote-hash <保存済みhash> で明示実行する。
同一ID・slugを保ち、公開状態への変更は行わない。照合と更新の間にCMSの編集を同時に行わない。
プラグインZIP: python tools/build_wordpress_plugin.py --output news-weave.zip。

復旧モードでは後から届いた原稿も照合専用になる。
全履歴を確認して finish-recovery --confirmed-history-reconciled で解除する。
未解決の投稿・画像がある場合は解除できない。
画像は resolve-media <package_id> --adopt-media-id <id>、または未作成確認後の
--confirmed-not-created で個別解決する。
