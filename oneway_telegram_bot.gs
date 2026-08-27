/**
 * =============================================================================
 * ONEWAY TELEGRAM BOT — OPTIMIZED ALBUM BATCH
 *
 * Telegram → Apps Script → Drive + Webapp
 *
 * Cú pháp:
 * IMEI 6 số;iOS;Pin;Lịch sử linh kiện
 * Serial Mac;CPU/GPU/RAM/Dung lượng
 *
 * Ví dụ:
 * 504265;26.5.2;91%;
 * 504265;26.5.2;91%;pin thay
 * CG9RM2W25M-A;10/10/16/512
 *
 * Sheet:
 * G = Trạng thái
 * H = Pin
 * I = KHÔNG ĐỘNG VÀO
 * J = KTV
 * K = Folder ảnh
 * L = Lịch sử linh kiện
 *
 * iOS chỉ dùng trong thông báo và App Print.
 *
 * -----------------------------------------------------------------------------
 * CƠ CHẾ ALBUM (bản sửa)
 *
 * Telegram gửi mỗi ảnh trong album thành một webhook riêng, gần như đồng thời,
 * và chỉ MỘT ảnh mang caption.
 *
 * 1. Mỗi execution đẩy ảnh của mình vào buffer album (ScriptProperties) NGAY
 *    lập tức, trước khi làm bất cứ việc nặng nào. Không ảnh nào bị mất kể cả
 *    khi execution đó thoát sớm.
 * 2. Caption được poll có giới hạn (ALBUM_CAPTION_WAIT_MS) thay vì sleep 1 lần.
 * 3. Chỉ MỘT execution (album owner) tìm máy + ghi sheet + tạo folder, các
 *    execution còn lại chờ context trong cache.
 * 4. Execution nào có caption sẽ CLAIM ảnh trong buffer (atomic) rồi upload,
 *    sau đó drain thêm vài vòng để hốt nốt ảnh của execution bị chết.
 * 5. Ảnh upload lỗi được trả lại buffer kèm số lần thử.
 * =============================================================================
 */

const BOT_TOKEN =
  "8437935389:AAE1ex-rsGff-UVwTVYd_2KSLQYffxMQb9Q";

const SPREADSHEET_ID =
  "1YTUV7lgh3d7dniv6KupCLqWMKl0rF69oRJmZRqz277Q";

const IPHONE_INSPECTION_LOG_SPREADSHEET_ID =
  "1583CqVhsEPSuyzSjemBk5F5m6ILWmUMHu-I_-Lgct1I";

const DRIVE_FOLDER_ID =
  "16odmlWqrJGNZ40SbyP59yEgYlVCaFqZU";

/*
 * Dán link Deploy Web app của Apps Script vào đây.
 * Link phải là dạng:
 * https://script.google.com/macros/s/.../exec
 */
const APPS_SCRIPT_WEB_APP_URL =
  "https://script.google.com/macros/s/AKfycbx7bL1jN2EdvOHFlRgYczMYbnx_wf_HUKLR6f9smjwrkr5q4wlTUDs3jaNdzBOKfZRI/exec";

const TELEGRAM_UPLOAD_RETRY_QUEUE_KEY =
  "telegram_upload_retry_queue_v1";

const TELEGRAM_UPLOAD_RETRY_MAX_ATTEMPTS =
  5;

/*
 * ===========================================================================
 * THAM SỐ ALBUM
 * ===========================================================================
 */

/* Prefix key buffer ảnh chờ upload của từng album. */
const ALBUM_PENDING_PHOTOS_PREFIX =
  "album_photos_";

/* Prefix key đánh dấu ảnh đã upload xong, chống upload trùng. */
const ALBUM_PHOTO_DONE_PREFIX =
  "photo_done_";

/* Thời gian tối đa chờ caption của album tới cache. */
const ALBUM_CAPTION_WAIT_MS =
  9000;

/* Thời gian tối đa chờ album owner ghi xong sheet + tạo folder. */
const ALBUM_CONTEXT_WAIT_MS =
  15000;

/* Nhịp poll chung cho caption và context. */
const ALBUM_POLL_INTERVAL_MS =
  400;

/* Ngân sách thời gian cho vòng drain gom ảnh còn sót. */
const ALBUM_DRAIN_BUDGET_MS =
  40000;

/* Nhịp poll của vòng drain. */
const ALBUM_DRAIN_INTERVAL_MS =
  1200;

/* Số vòng drain rỗng liên tiếp thì dừng. */
const ALBUM_DRAIN_IDLE_ROUNDS =
  3;

/* Số lần thử lại tối đa cho một ảnh trong buffer album. */
const ALBUM_PHOTO_MAX_ATTEMPTS =
  3;

/* Timeout mặc định khi giành script lock. */
const SCRIPT_LOCK_TIMEOUT_MS =
  30000;


function doGet() {
  return ContentService
    .createTextOutput(
      "Oneway Batch Bot Active!"
    )
    .setMimeType(
      ContentService.MimeType.TEXT
    );
}


/**
 * WEBHOOK TELEGRAM.
 *
 * Mỗi ảnh trong album là MỘT update riêng của Telegram, chỉ một ảnh mang
 * caption. Vì vậy mỗi execution chỉ lo đúng ảnh của chính nó:
 *   1. Ảnh nào có caption thì cache caption theo media_group_id.
 *   2. Ảnh không caption thì chờ caption xuất hiện trong cache.
 *   3. Việc tìm máy / tạo folder / ghi sheet chỉ chạy MỘT lần cho cả album
 *      (khoá script + cache context theo media_group_id).
 *   4. Upload ảnh của chính execution này, KHÔNG gom buffer, KHÔNG drain.
 */
function doPost(e) {
  const output = ContentService.createTextOutput(JSON.stringify({ok: true}))
    .setMimeType(ContentService.MimeType.JSON);

  let chatId = null;
  let messageId = null;
  let mediaGroupId = "";
  const isRetryExecution = Boolean(e && e.retryTelegramUpload);

  try {
    if (!e || !e.postData || !e.postData.contents) return output;

    const update = JSON.parse(e.postData.contents);
    if (!update.message) return output;

    const msg = update.message;
    chatId = msg.chat.id;
    messageId = msg.message_id;
    mediaGroupId = String(msg.media_group_id || "");

    const photos = msg.photo || [];

    /* Tin nhắn có cú pháp nhưng không có ảnh. */
    if (photos.length === 0) {
      const text = String(msg.text || "").trim();
      if (/^[A-Za-z0-9]/.test(text) && claimOnce("warning_" + update.update_id)) {
        sendTelegramMessage(chatId,
          "⚠️ Vui lòng gửi KÈM HÌNH ẢNH kiểm định máy!\n\n" +
          "👉 Cú pháp:\nIMEI 6 số;iOS;Pin;Lịch sử linh kiện\n\n" +
          "Hoặc Macbook:\nSerial;CPU/GPU/RAM/Dung lượng\n\n" +
          "Ví dụ:\n504265;26.5.2;91%;pin thay\nLGDK7LQN72;8/8/16/256");
      }
      return output;
    }

    const largestPhoto = photos[photos.length - 1];
    const rawCaption = String(msg.caption || msg.text || "").trim();

    const fromUser = msg.from || {};
    let ktvName = [fromUser.first_name || "", fromUser.last_name || ""].join(" ").trim();
    if (!ktvName) ktvName = fromUser.username ? "@" + fromUser.username : "KTV Oneway";

    const photoInfo = {
      updateId: update.update_id,
      messageId: messageId,
      fileId: largestPhoto.file_id,
      fileUniqueId: largestPhoto.file_unique_id || largestPhoto.file_id,
      attempts: 0
    };

    const cache = CacheService.getScriptCache();

    /*
     * BƯỚC 1 — CAPTION THEO ALBUM ID.
     * Execution mang caption ghi caption vào cache ngay. Execution không
     * caption poll cache tới khi thấy caption. Tuyệt đối KHÔNG giữ khoá ở
     * đoạn này, nếu không execution mang caption sẽ bị kẹt phía sau.
     */
    const caption = mediaGroupId
      ? waitForAlbumCaption_(cache, mediaGroupId, rawCaption)
      : rawCaption;

    /* Không có caption hợp lệ thì bỏ qua ảnh này, không báo lỗi ồn ào. */
    if (!caption || !/^[A-Za-z0-9]/.test(caption)) {
      return output;
    }

    /* Chống xử lý trùng một update. */
    if (!isRetryExecution && !claimOnce("photo_update_" + update.update_id)) {
      return output;
    }

    /* BƯỚC 2 — KIỂM TRA CÚ PHÁP. */
    const parsed = parseCaption(caption);

    const missing = [];
    if (!parsed.imei || (parsed.isMacCaption ? parsed.imei.length < 6
                                             : !/^\d{6}$/.test(parsed.imei))) {
      missing.push(parsed.isMacCaption ? "Serial Mac" : "IMEI đúng 6 số");
    }
    if (!parsed.isMacCaption && !parsed.os) missing.push("Phiên bản iOS");
    if (!parsed.isMacCaption && !parsed.pin) missing.push("Dung lượng pin");
    if (parsed.isMacCaption && !parsed.parts) missing.push("Thông số Mac");

    if (missing.length > 0) {
      const errorKey = mediaGroupId ? "input_error_" + mediaGroupId
                                    : "input_error_" + update.update_id;
      if (claimOnce(errorKey)) {
        sendTelegramMessage(chatId,
          "⚠️ Thiếu hoặc sai thông tin: " + missing.join(", ") + "!\n\n" +
          "👉 Cú pháp:\nIMEI 6 số;iOS;Pin;Lịch sử linh kiện\n\n" +
          "Hoặc Macbook:\nSerial;CPU/GPU/RAM/Dung lượng\n\n" +
          "Ví dụ:\n504265;26.5.2;91%;pin thay\nLGDK7LQN72;8/8/16/256");
      }
      return output;
    }

    /*
     * BƯỚC 3 — CONTEXT ALBUM (tìm máy + folder + ghi sheet).
     * Chỉ chạy một lần cho cả album. Các execution còn lại đọc lại context
     * từ cache. Khoá chỉ bao quanh đoạn này, KHÔNG bao quanh upload vì
     * reserveNextPhotoFileNames_ tự lấy khoá bên trong.
     */
    let context = mediaGroupId ? readAlbumInspectionContext_(cache, mediaGroupId) : null;
    let deviceMissingMessage = "";

    if (!context) {
      context = withScriptLock(function() {
        /* Đọc lại trong khoá: execution khác có thể vừa tạo xong. */
        if (mediaGroupId) {
          const fresh = readAlbumInspectionContext_(cache, mediaGroupId);
          if (fresh && fresh.fullImei) return fresh;
        }

        const device = findDeviceOnce(parsed.imei);
        if (!device.success) {
          deviceMissingMessage = device.message;
          return null;
        }

        const folderName = device.fullImei + " - " + sanitizeFolderName(device.phoneName);
        const folder = getOrCreateFolder(DRIVE_FOLDER_ID, folderName);
        const url = folder.getUrl();

        updateInspectionRowOnce(device, parsed.pin, parsed.parts, ktvName, url);

        const webappMetadata = notifyTelegramInspectionToWebapp(
          caption, ktvName, fromUser.username || "", url, device.phoneName);

        const built = {
          fullImei: device.fullImei,
          phoneName: device.phoneName,
          sheetName: device.sheetName || "",
          row: device.row || 0,
          folderId: folder.getId(),
          folderUrl: url,
          inspectionId: String(webappMetadata.inspectionId || "")
        };

        if (mediaGroupId) saveAlbumInspectionContext_(cache, mediaGroupId, built);
        return built;
      });
    }

    if (!context || !context.fullImei) {
      const searchErrorKey = mediaGroupId ? "search_error_" + mediaGroupId
                                          : "search_error_" + update.update_id;
      if (deviceMissingMessage && claimOnce(searchErrorKey)) {
        sendTelegramMessage(chatId, "❌ " + deviceMissingMessage);
      }
      return output;
    }

    const device = {
      success: true,
      fullImei: context.fullImei,
      phoneName: context.phoneName || "Thiết bị Oneway",
      sheetName: context.sheetName || "",
      row: context.row || 0
    };
    const deviceFolder = DriveApp.getFolderById(context.folderId);
    const folderUrl = String(context.folderUrl || "");
    const inspectionId = String(context.inspectionId || "");

    /* BƯỚC 4 — THÔNG BÁO TRẠNG THÁI + REACTION. */
    const statusKey = "inspection_status_" + (mediaGroupId || update.update_id);
    registerTelegramInspectionStatus(statusKey, chatId, {
      ktvName: ktvName,
      phoneName: device.phoneName,
      imei: device.fullImei,
      os: parsed.os,
      pin: parsed.pin,
      parts: parsed.parts || "",
      isMacCaption: parsed.isMacCaption
    }, rawCaption ? messageId : null);

    if (rawCaption && claimOnce("reaction_" + statusKey)) {
      try {
        sendReaction(chatId, messageId, "👍");
      } catch (reactionError) {
        console.warn("Không thả được reaction Telegram: " +
          (reactionError.message || reactionError));
      }
    }

    /*
     * BƯỚC 5 — UPLOAD ẢNH CỦA CHÍNH EXECUTION NÀY.
     * Mỗi update lo đúng một ảnh, nên cả album đủ ảnh mà không cần buffer.
     * Phải nằm NGOÀI khoá script.
     */
    const uploadResult = uploadPhotosBatch(
      [photoInfo], device.fullImei, deviceFolder, folderUrl, caption,
      ktvName, fromUser.username || "", device.phoneName, inspectionId);

    if (uploadResult.successful.length === 0) {
      const uploadError = new Error("Không upload được ảnh lên Drive.");
      uploadError.transient = uploadResult.failed.some(function(failure) {
        return Boolean(failure && failure.transient);
      });
      throw uploadError;
    }

    console.log("Album " + (mediaGroupId || "single") + ": đã upload ảnh " +
      photoInfo.fileUniqueId + ".");

  } catch (error) {
    console.error(error.stack || error);

    if (e && e.postData && e.postData.contents &&
        shouldRetryTelegramUpload_(error) &&
        enqueueTelegramUploadRetry_(e.postData.contents, error)) {
      return output;
    }

    if (chatId) {
      const runtimeErrorKey = mediaGroupId ? "runtime_error_" + mediaGroupId
                                           : "runtime_error_" + messageId;
      try {
        if (claimOnce(runtimeErrorKey)) {
          sendTelegramMessage(chatId,
            "❌ BOT XỬ LÝ THẤT BẠI!\n\n" + String(error.message || error));
        }
      } catch (sendError) {
        console.error(sendError);
      }
    }
  }

  return output;
}


/*
 * =============================================================================
 * BUFFER ẢNH ALBUM
 *
 * Dùng ScriptProperties chứ không dùng Cache vì cache có thể bị evict và
 * không đảm bảo đọc-ghi atomic giữa các execution song song.
 * =============================================================================
 */

function albumPendingPhotosKey_(
  mediaGroupId
) {
  return (
    ALBUM_PENDING_PHOTOS_PREFIX +
    safeKey(
      mediaGroupId
    )
  );
}


function readAlbumPendingPhotos_(
  mediaGroupId
) {
  const raw =
    PropertiesService
      .getScriptProperties()
      .getProperty(
        albumPendingPhotosKey_(
          mediaGroupId
        )
      );

  if (!raw) {
    return [];
  }

  try {
    const parsed =
      JSON.parse(
        raw
      );

    return Array.isArray(
      parsed
    )
      ? parsed
      : [];
  } catch (
    error
  ) {
    return [];
  }
}


/**
 * Thêm ảnh vào buffer album. Dedupe theo fileUniqueId.
 */
function appendAlbumPendingPhoto_(
  mediaGroupId,
  photoInfo
) {
  if (
    !mediaGroupId ||
    !photoInfo
  ) {
    return [];
  }

  return withScriptLock(
    function() {
      const properties =
        PropertiesService
          .getScriptProperties();

      const list =
        readAlbumPendingPhotos_(
          mediaGroupId
        );

      const exists =
        list.some(
          function(item) {
            return Boolean(
              item &&
              item.fileUniqueId ===
                photoInfo.fileUniqueId
            );
          }
        );

      if (!exists) {
        list.push(
          photoInfo
        );

        properties.setProperty(
          albumPendingPhotosKey_(
            mediaGroupId
          ),
          JSON.stringify(
            list
          )
        );
      }

      return list;
    }
  );
}


/**
 * Lấy toàn bộ ảnh đang chờ của album và xoá khỏi buffer trong cùng một lock.
 * Bảo đảm hai execution song song không upload trùng một ảnh.
 */
function claimAlbumPendingPhotos_(
  mediaGroupId
) {
  if (!mediaGroupId) {
    return [];
  }

  return withScriptLock(
    function() {
      const list =
        readAlbumPendingPhotos_(
          mediaGroupId
        );

      if (!list.length) {
        return [];
      }

      PropertiesService
        .getScriptProperties()
        .deleteProperty(
          albumPendingPhotosKey_(
            mediaGroupId
          )
        );

      return list;
    }
  );
}


/**
 * Xoá buffer khi caption sai cú pháp hoặc không tìm thấy máy.
 */
function discardAlbumPendingPhotos_(
  mediaGroupId
) {
  if (!mediaGroupId) {
    return;
  }

  try {
    withScriptLock(
      function() {
        PropertiesService
          .getScriptProperties()
          .deleteProperty(
            albumPendingPhotosKey_(
              mediaGroupId
            )
          );
      }
    );
  } catch (
    error
  ) {
    console.warn(
      "Không xoá được buffer album " +
      mediaGroupId +
      ": " +
      String(
        error.message ||
        error
      )
    );
  }
}


/**
 * Trả ảnh upload lỗi về buffer để execution khác thử lại.
 * Bỏ hẳn ảnh đã thử quá ALBUM_PHOTO_MAX_ATTEMPTS lần.
 */
function requeueFailedAlbumPhotos_(
  mediaGroupId,
  failures
) {
  failures.forEach(
    function(failure) {
      if (
        !failure ||
        !failure.photo ||
        !failure.transient
      ) {
        return;
      }

      const photo =
        failure.photo;

      const attempts =
        Number(
          photo.attempts || 0
        ) + 1;

      if (
        attempts >
        ALBUM_PHOTO_MAX_ATTEMPTS
      ) {
        console.warn(
          "Bỏ ảnh album sau " +
          ALBUM_PHOTO_MAX_ATTEMPTS +
          " lần lỗi: " +
          photo.fileUniqueId
        );

        return;
      }

      photo.attempts =
        attempts;

      appendAlbumPendingPhoto_(
        mediaGroupId,
        photo
      );
    }
  );
}


/**
 * Vòng gom ảnh còn sót của album.
 * Trả về số ảnh upload thành công thêm được.
 */
function drainAlbumPendingPhotos_(
  mediaGroupId,
  startedAt,
  context
) {
  let uploaded =
    0;

  let idleRounds =
    0;

  while (
    idleRounds < ALBUM_DRAIN_IDLE_ROUNDS &&
    (
      Date.now() - startedAt
    ) < ALBUM_DRAIN_BUDGET_MS
  ) {
    Utilities.sleep(
      ALBUM_DRAIN_INTERVAL_MS
    );

    const pending =
      claimAlbumPendingPhotos_(
        mediaGroupId
      );

    if (!pending.length) {
      idleRounds++;
      continue;
    }

    idleRounds =
      0;

    const result =
      uploadPhotosBatch(
        pending,
        context.device.fullImei,
        context.deviceFolder,
        context.folderUrl,
        context.caption,
        context.ktvName,
        context.telegramUsername,
        context.device.phoneName,
        context.inspectionId
      );

    uploaded +=
      result.successful.length;

    if (
      result.failed.length > 0
    ) {
      requeueFailedAlbumPhotos_(
        mediaGroupId,
        result.failed
      );
    }
  }

  return uploaded;
}


/*
 * =============================================================================
 * CHỐNG UPLOAD TRÙNG THEO TỪNG ẢNH
 * =============================================================================
 */

function isPhotoAlreadyUploaded_(
  fileUniqueId
) {
  if (!fileUniqueId) {
    return false;
  }

  return Boolean(
    PropertiesService
      .getScriptProperties()
      .getProperty(
        ALBUM_PHOTO_DONE_PREFIX +
        safeKey(
          fileUniqueId
        )
      )
  );
}


function markPhotoUploaded_(
  fileUniqueId
) {
  if (!fileUniqueId) {
    return;
  }

  PropertiesService
    .getScriptProperties()
    .setProperty(
      ALBUM_PHOTO_DONE_PREFIX +
      safeKey(
        fileUniqueId
      ),
      String(
        Date.now()
      )
    );
}


/*
 * =============================================================================
 * CHỜ CAPTION VÀ CONTEXT ALBUM
 * =============================================================================
 */

function albumCaptionCacheKey_(
  mediaGroupId
) {
  return (
    "album_caption_" +
    safeKey(
      mediaGroupId
    )
  );
}


/**
 * Ảnh mang caption thì cache ngay cho cả album.
 * Ảnh không caption thì poll tới khi thấy caption hoặc hết giờ.
 */
function waitForAlbumCaption_(
  cache,
  mediaGroupId,
  rawCaption
) {
  const cacheKey =
    albumCaptionCacheKey_(
      mediaGroupId
    );

  if (rawCaption) {
    cache.put(
      cacheKey,
      rawCaption,
      1800
    );

    return rawCaption;
  }

  const deadline =
    Date.now() +
    ALBUM_CAPTION_WAIT_MS;

  while (
    Date.now() < deadline
  ) {
    const cached =
      cache.get(
        cacheKey
      );

    if (cached) {
      return cached;
    }

    Utilities.sleep(
      ALBUM_POLL_INTERVAL_MS
    );
  }

  return cache.get(
    cacheKey
  ) || "";
}


/**
 * Chờ album owner ghi xong sheet và tạo folder.
 */
function waitForAlbumInspectionContext_(
  cache,
  mediaGroupId
) {
  const deadline =
    Date.now() +
    ALBUM_CONTEXT_WAIT_MS;

  while (
    Date.now() < deadline
  ) {
    const context =
      readAlbumInspectionContext_(
        cache,
        mediaGroupId
      );

    if (
      context &&
      context.fullImei
    ) {
      return context;
    }

    Utilities.sleep(
      ALBUM_POLL_INTERVAL_MS
    );
  }

  console.warn(
    "Hết giờ chờ context album " +
    mediaGroupId +
    ", execution này sẽ tự xử lý."
  );

  return null;
}


/**
 * ĐỌC CAPTION
 */
function parseCaption(
  text
) {
  const fields =
    String(
      text || ""
    )
      .split(";")
      .map(function(field) {
        return field.trim();
      });

  if (fields.length === 2) {
    return {
      imei: String(fields[0] || "")
        .toUpperCase()
        .replace(/[^A-Z0-9]/g, ""),
      os: "",
      pin: "",
      parts: String(fields[1] || "").trim(),
      isMacCaption: true
    };
  }

  const imei =
    String(
      fields[0] || ""
    )
      .replace(
        /\D/g,
        ""
      );

  const os =
    String(
      fields[1] || ""
    ).trim();

  let pin =
    String(
      fields[2] || ""
    )
      .replace(
        /\s/g,
        ""
      )
      .trim();

  if (
    /^\d{1,3}$/.test(
      pin
    )
  ) {
    pin += "%";
  }

  const parts =
    fields.length > 3
      ? fields
          .slice(3)
          .join(";")
          .trim()
      : "";

  return {
    imei: imei,
    os: os,
    pin: pin,
    parts: parts,
    isMacCaption: false
  };
}

/**
 * TÌM MÁY MỘT LẦN
 */
function findDeviceOnce(
  scanCode
) {
  const spreadsheet =
    withSpreadsheetRetry(
      function() {
        return SpreadsheetApp.openById(
          SPREADSHEET_ID
        );
      }
    );

  const code = String(scanCode || "")
    .toUpperCase()
    .replace(/[^A-Z0-9]/g, "");
  if (/[A-Z]/.test(code)) {
    return findMacbookDeviceOnce(
      spreadsheet,
      code
    );
  }
  const numericCode = code.replace(/\D/g, "");
  const results =
    withSpreadsheetRetry(
      function() {
        return spreadsheet
          .createTextFinder(code)
          .matchCase(false)
          .matchEntireCell(false)
          .findAll();
      }
    );

  const candidates = [];

  results.forEach(function(cell) {
    const column = cell.getColumn();
    if (column !== 2 && column !== 3) return;

    const sheet = cell.getSheet();
    const sheetName = sheet.getName();
    const isMacbookSheet = sheetName === "Macbook";
    const row = cell.getRow();
    const values = sheet.getRange(row, 1, 1, 12).getDisplayValues()[0];
    const fullImei = isMacbookSheet
      ? String(values[1] || "").toUpperCase().replace(/[^A-Z0-9]/g, "")
      : String(values[1] || "").replace(/\D/g, "");
    const shortImei = isMacbookSheet
      ? ""
      : String(values[2] || "").replace(/\D/g, "");
    const isMatch = isMacbookSheet
      ? fullImei === code
      : numericCode.length === 6
        ? shortImei === numericCode || fullImei.slice(-6) === numericCode
        : fullImei === numericCode;
    if (!isMatch) return;

    const phoneName =
      String(values[isMacbookSheet ? 2 : 3] || "").trim();
    const warehouseReturn = String(values[isMacbookSheet ? 7 : 8] || "").trim();
    const driveFolderUrl = String(values[isMacbookSheet ? 9 : 10] || "").trim();
    const hasPhoneName = phoneName !== "";
    const hasWarehouseReturn = warehouseReturn.indexOf("ĐVK") !== -1;
    const hasDriveLink = driveFolderUrl.indexOf("drive.google.com") !== -1;
    const score = (hasPhoneName ? 4 : 0) + (hasWarehouseReturn && hasDriveLink
      ? 3
      : hasDriveLink
        ? 2
        : hasWarehouseReturn
          ? 1
          : 0);

    candidates.push({
      success: true,
      sheet: sheet,
      sheetName: sheetName,
      row: row,
      fullImei: fullImei || shortImei,
      phoneName: phoneName || "Thiết bị Oneway",
      status: String(values[6] || "").trim().toLowerCase(),
      score: score
    });
  });

  if (!candidates.length) {
    return {
      success: false,
      message: "Không tìm thấy IMEI [" + code + "] trong cột B hoặc C!"
    };
  }

  const distinctImeis = {};
  candidates.forEach(function(candidate) {
    distinctImeis[candidate.fullImei] = true;
  });
  if (code.length === 6 && Object.keys(distinctImeis).length > 1) {
    return {
      success: false,
      message: "Có nhiều máy trùng 6 số cuối [" + code + "], vui lòng nhập IMEI đầy đủ."
    };
  }

  candidates.sort(function(left, right) {
    return right.score - left.score || right.row - left.row;
  });
  return candidates[0];
}

function findMacbookDeviceOnce(
  spreadsheet,
  code
) {
  const sheet =
    spreadsheet.getSheetByName(
      "Macbook"
    );

  if (!sheet) {
    return {
      success: false,
      message: "Không tìm thấy tab Macbook!"
    };
  }

  const values =
    sheet
      .getDataRange()
      .getDisplayValues();

  const candidates = [];

  values.forEach(function(rowValues, rowIndex) {
    const serial =
      String(rowValues[1] || "")
        .toUpperCase()
        .replace(/[^A-Z0-9]/g, "");

    if (serial !== code) return;

    const phoneName =
      String(rowValues[2] || "").trim();

    const warehouseReturn =
      String(rowValues[7] || "").trim();

    const driveFolderUrl =
      String(rowValues[9] || "").trim();

    const hasPhoneName =
      phoneName !== "";

    const hasWarehouseReturn =
      warehouseReturn.indexOf("ĐVK") !== -1;

    const hasDriveLink =
      driveFolderUrl.indexOf("drive.google.com") !== -1;

    const score =
      (hasPhoneName ? 4 : 0) +
      (hasWarehouseReturn && hasDriveLink
        ? 3
        : hasDriveLink
          ? 2
          : hasWarehouseReturn
            ? 1
            : 0);

    candidates.push({
      success: true,
      sheet: sheet,
      sheetName: "Macbook",
      row: rowIndex + 1,
      fullImei: serial,
      phoneName: phoneName || "Thiết bị Oneway",
      status: String(rowValues[6] || "").trim().toLowerCase(),
      score: score
    });
  });

  if (!candidates.length) {
    return {
      success: false,
      message: "Không tìm thấy serial Macbook [" + code + "] trong cột B!"
    };
  }

  candidates.sort(function(left, right) {
    return right.score - left.score || right.row - left.row;
  });

  return candidates[0];
}

/**
 * UPLOAD NHIỀU ẢNH
 *
 * Khác bản cũ:
 * - Bỏ qua ảnh đã upload xong trước đó (chống trùng khi retry).
 * - Đặt trước toàn bộ tên file trong MỘT lần lock thay vì quét folder cho
 *   từng ảnh bên trong lock.
 * - Ghi file Drive ngoài lock nên nhiều execution album chạy song song được.
 */
function uploadPhotosBatch(
  photos,
  imei,
  folder,
  folderUrl,
  caption,
  ktvName,
  telegramUsername,
  deviceName,
  inspectionId
) {
  const successful =
    [];

  const failed =
    [];

  const downloadJobs =
    [];

  photos.forEach(
    function(photo) {
      if (
        !photo ||
        !photo.fileId
      ) {
        return;
      }

      if (
        isPhotoAlreadyUploaded_(
          photo.fileUniqueId
        )
      ) {
        console.log(
          "Bỏ qua ảnh đã upload: " +
          photo.fileUniqueId
        );

        return;
      }

      downloadJobs.push(
        {
          photo: photo
        }
      );
    }
  );

  if (
    downloadJobs.length === 0
  ) {
    return {
      successful: successful,
      failed: failed
    };
  }

  const validJobs =
    [];

  const fileInfoRequests =
    downloadJobs.map(
      function(job) {
        return {
          url:
            "https://api.telegram.org/bot" +
            BOT_TOKEN +
            "/getFile?file_id=" +
            encodeURIComponent(
              job.photo.fileId
            ),

          method:
            "get",

          muteHttpExceptions:
            true
        };
      }
    );

  try {
    const fileInfoResponses =
      UrlFetchApp.fetchAll(
        fileInfoRequests
      );

    fileInfoResponses.forEach(
      function(response, index) {
        const job =
          downloadJobs[index];

        try {
          const data =
            JSON.parse(
              response.getContentText() ||
              "{}"
            );

          if (
            response.getResponseCode() !== 200 ||
            !data.ok ||
            !data.result ||
            !data.result.file_path
          ) {
            throw new Error(
              "Không có file_path."
            );
          }

          job.filePath =
            data.result.file_path;

          validJobs.push(
            job
          );

        } catch (
          error
        ) {
          const message =
            String(
              error.message ||
              error
            );

          failed.push(
            {
              photo:
                job.photo,

              error:
                message,

              transient:
                isTransientTelegramFileError(
                  error
                ) ||
                response.getResponseCode() >= 500
            }
          );
        }
      }
    );

  } catch (
    error
  ) {
    downloadJobs.forEach(
      function(job) {
        failed.push(
          {
            photo:
              job.photo,

            error:
              String(
                error.message ||
                error
              ),

            transient:
              isTransientTelegramFileError(
                error
              )
          }
        );
      }
    );
  }

  if (
    validJobs.length === 0
  ) {
    return {
      successful: successful,
      failed: failed
    };
  }

  /*
   * DriveApp không có createFile batch, nhưng tải ảnh Telegram thì gom fetchAll
   * để album nhiều ảnh không bị chậm do tải tuần tự.
   */
  const imageRequests =
    validJobs.map(
      function(job) {
        return {
          url:
            "https://api.telegram.org/file/bot" +
            BOT_TOKEN +
            "/" +
            job.filePath,

          method:
            "get",

          muteHttpExceptions:
            true
        };
      }
    );

  let imageResponses =
    [];

  try {
    imageResponses =
      UrlFetchApp.fetchAll(
        imageRequests
      );
  } catch (
    error
  ) {
    validJobs.forEach(
      function(job) {
        failed.push(
          {
            photo:
              job.photo,

            error:
              String(
                error.message ||
                error
              ),

            transient:
              isTransientTelegramFileError(
                error
              )
          }
        );
      }
    );

    return {
      successful: successful,
      failed: failed
    };
  }

  /*
   * Đặt trước tên file cho cả lô trong một lần lock duy nhất.
   */
  let reservedNames =
    [];

  try {
    reservedNames =
      reserveNextPhotoFileNames_(
        folder,
        validJobs.length
      );
  } catch (
    error
  ) {
    validJobs.forEach(
      function(job) {
        failed.push(
          {
            photo:
              job.photo,

            error:
              String(
                error.message ||
                error
              ),

            transient:
              true
          }
        );
      }
    );

    return {
      successful: successful,
      failed: failed
    };
  }

  imageResponses.forEach(
    function(response, index) {
      const job =
        validJobs[index];

      try {
        if (
          response
            .getResponseCode() !==
          200
        ) {
          throw new Error(
            "HTTP " +
            response
              .getResponseCode()
          );
        }

        const fileName =
          reservedNames[index];

        const webappBlob =
          response
            .getBlob()
            .setName(
              fileName
            );

        createDriveFileWithRetry_(
          folder,
          webappBlob
        );

        markPhotoUploaded_(
          job.photo.fileUniqueId
        );

        queueTelegramPhotoToWebapp(
          webappBlob,
          caption,
          ktvName,
          telegramUsername,
          folderUrl,
          fileName,
          deviceName,
          inspectionId
        );

        successful.push(
          job.photo
        );

      } catch (
        error
      ) {
        failed.push(
          {
            photo:
              job.photo,

            error:
              String(
                error.message ||
                error
              ),

            transient:
              isTransientTelegramFileError(
                error
              ) ||
              response.getResponseCode() >= 500 ||
              isTransientDriveError_(
                error
              ) ||
              isTransientLockError_(
                error
              )
          }
        );
      }
    }
  );

  return {
    successful: successful,
    failed: failed
  };
}

function fetchTelegramFileInfoWithRetry(
  fileId
) {
  const url =
    "https://api.telegram.org/bot" +
    BOT_TOKEN +
    "/getFile?file_id=" +
    encodeURIComponent(
      fileId
    );

  let lastError = null;

  for (
    let attempt = 0;
    attempt < 4;
    attempt++
  ) {
    try {
      const response =
        UrlFetchApp.fetch(
          url,
          {
            method: "get",
            muteHttpExceptions: true
          }
        );

      const data =
        JSON.parse(
          response.getContentText() ||
          "{}"
        );

      if (
        !data.ok ||
        !data.result ||
        !data.result.file_path
      ) {
        throw new Error(
          "Không có file_path."
        );
      }

      return data.result.file_path;

    } catch (
      error
    ) {
      lastError =
        error;

      if (
        !isTransientTelegramFileError(
          error
        ) ||
        attempt === 3
      ) {
        throw error;
      }

      Utilities.sleep(
        500 * (attempt + 1)
      );
    }
  }

  throw lastError;
}

function fetchTelegramFileWithRetry(
  filePath
) {
  const url =
    "https://api.telegram.org/file/bot" +
    BOT_TOKEN +
    "/" +
    filePath;

  let lastError = null;

  for (
    let attempt = 0;
    attempt < 3;
    attempt++
  ) {
    try {
      return UrlFetchApp.fetch(
        url,
        {
          method: "get",
          muteHttpExceptions: true
        }
      );
    } catch (
      error
    ) {
      lastError =
        error;

      if (
        !isTransientTelegramFileError(
          error
        ) ||
        attempt === 2
      ) {
        throw error;
      }

      Utilities.sleep(
        500 * (attempt + 1)
      );
    }
  }

  throw lastError;
}

function createDriveFileWithRetry_(
  folder,
  blob
) {
  let lastError = null;

  for (
    let attempt = 0;
    attempt < 3;
    attempt++
  ) {
    try {
      return folder.createFile(
        blob
      );
    } catch (
      error
    ) {
      lastError =
        error;

      if (
        !isTransientDriveError_(
          error
        ) ||
        attempt === 2
      ) {
        throw error;
      }

      Utilities.sleep(
        700 * (attempt + 1)
      );
    }
  }

  throw lastError;
}

function isTransientTelegramFileError(
  error
) {
  const message =
    String(
      (
        error &&
        error.message
      ) ||
      error ||
      ""
    );

  return /Address unavailable|DNS error|timed out|SocketException|Exception: Request failed/i.test(
    message
  );
}

function isTransientDriveError_(
  error
) {
  const message =
    String(
      (
        error &&
        error.message
      ) ||
      error ||
      ""
    );

  return /Service Drive failed|Internal error|Backend Error|timed out|Address unavailable|Exception: We're sorry/i.test(
    message
  );
}

/**
 * Lỗi giành lock cũng là lỗi tạm thời — trước đây bị bỏ sót nên ảnh album
 * bị drop thay vì được thử lại.
 */
function isTransientLockError_(
  error
) {
  const message =
    String(
      (
        error &&
        error.message
      ) ||
      error ||
      ""
    );

  return /lock|Lock|timeout|Timeout|hết thời gian chờ/.test(
    message
  ) &&
  /lock|Lock/.test(
    message
  );
}

function shouldRetryTelegramUpload_(
  error
) {
  return Boolean(
    error &&
    error.transient
  ) ||
  isTransientTelegramFileError(
    error
  ) ||
  isTransientDriveError_(
    error
  ) ||
  isTransientLockError_(
    error
  );
}

function readTelegramUploadRetryQueue_() {
  const raw =
    PropertiesService
      .getScriptProperties()
      .getProperty(
        TELEGRAM_UPLOAD_RETRY_QUEUE_KEY
      );

  if (!raw) {
    return [];
  }

  try {
    const parsed =
      JSON.parse(
        raw
      );

    return Array.isArray(
      parsed
    )
      ? parsed
      : [];
  } catch (
    error
  ) {
    return [];
  }
}

function saveTelegramUploadRetryQueue_(
  queue
) {
  PropertiesService
    .getScriptProperties()
    .setProperty(
      TELEGRAM_UPLOAD_RETRY_QUEUE_KEY,
      JSON.stringify(
        queue.slice(
          -20
        )
      )
    );
}

function enqueueTelegramUploadRetry_(
  payload,
  error
) {
  const queue =
    readTelegramUploadRetryQueue_();

  let updateId =
    "";

  try {
    const update =
      JSON.parse(
        payload
      );

    updateId =
      String(
        update.update_id || ""
      );
  } catch (
    parseError
  ) {
    updateId =
      "";
  }

  const key =
    updateId ||
    String(
      Utilities.getUuid()
    );

  let job =
    null;

  queue.forEach(
    function(item) {
      if (
        item &&
        item.key === key
      ) {
        job =
          item;
      }
    }
  );

  if (!job) {
    job = {
      key:
        key,

      payload:
        payload,

      attempts:
        0
    };

    queue.push(
      job
    );
  }

  job.attempts =
    Number(
      job.attempts || 0
    ) + 1;

  job.lastError =
    String(
      (
        error &&
        error.message
      ) ||
      error ||
      ""
    ).slice(
      0,
      500
    );

  if (
    job.attempts >
    TELEGRAM_UPLOAD_RETRY_MAX_ATTEMPTS
  ) {
    saveTelegramUploadRetryQueue_(
      queue.filter(
        function(item) {
          return item.key !== key;
        }
      )
    );

    return false;
  }

  job.nextRunAt =
    Date.now() +
    Math.min(
      60000 * job.attempts,
      5 * 60000
    );

  saveTelegramUploadRetryQueue_(
    queue
  );

  scheduleTelegramUploadRetryTrigger_();

  console.warn(
    "Đã đưa ảnh Telegram vào retry queue: " +
    key +
    " lần " +
    job.attempts +
    ". Lỗi: " +
    job.lastError
  );

  return true;
}

function scheduleTelegramUploadRetryTrigger_() {
  let triggers =
    [];

  try {
    triggers =
      ScriptApp.getProjectTriggers();
  } catch (
    error
  ) {
    console.warn(
      "Không đủ quyền tạo trigger retry Telegram, sẽ chờ chạy thủ công: " +
      String(
        (
          error &&
          error.message
        ) ||
        error
      )
    );

    return;
  }

  const exists =
    triggers.some(
      function(trigger) {
        return trigger.getHandlerFunction &&
          trigger.getHandlerFunction() ===
            "runTelegramUploadRetries";
      }
    );

  if (exists) {
    return;
  }

  try {
    ScriptApp
      .newTrigger(
        "runTelegramUploadRetries"
      )
      .timeBased()
      .after(
        60 * 1000
      )
      .create();
  } catch (
    error
  ) {
    console.warn(
      "Không tạo được trigger retry Telegram, sẽ chờ chạy thủ công: " +
      String(
        (
          error &&
          error.message
        ) ||
        error
      )
    );
  }
}

function clearTelegramUploadRetryTriggers_() {
  let triggers =
    [];

  try {
    triggers =
      ScriptApp.getProjectTriggers();
  } catch (
    error
  ) {
    console.warn(
      "Không đủ quyền xoá trigger retry Telegram, vẫn tiếp tục chạy retry: " +
      String(
        (
          error &&
          error.message
        ) ||
        error
      )
    );

    return;
  }

  triggers
    .forEach(
      function(trigger) {
        if (
          trigger.getHandlerFunction &&
          trigger.getHandlerFunction() ===
            "runTelegramUploadRetries"
        ) {
          try {
            ScriptApp.deleteTrigger(
              trigger
            );
          } catch (
            error
          ) {
            console.warn(
              "Không xoá được trigger retry Telegram: " +
              String(
                (
                  error &&
                  error.message
                ) ||
                error
              )
            );
          }
        }
      }
    );
}

function runTelegramUploadRetries() {
  clearTelegramUploadRetryTriggers_();

  const now =
    Date.now();

  const queue =
    readTelegramUploadRetryQueue_();

  const remaining =
    [];

  const due =
    [];

  queue.forEach(
    function(job) {
      if (
        !job ||
        Number(
          job.nextRunAt || 0
        ) > now
      ) {
        remaining.push(
          job
        );
        return;
      }

      due.push(
        job
      );
    }
  );

  saveTelegramUploadRetryQueue_(
    remaining
  );

  due.forEach(
    function(job) {
      try {
        doPost({
          postData: {
            contents:
              job.payload
          },

          retryTelegramUpload:
            true
        });
      } catch (
        error
      ) {
        job.lastError =
          String(
            error.message ||
            error
          );

        enqueueTelegramUploadRetry_(
          job.payload,
          error
        );
      }
    }
  );

  if (
    readTelegramUploadRetryQueue_()
      .length > 0
  ) {
    scheduleTelegramUploadRetryTrigger_();
  }
}


/**
 * ĐẶT TRƯỚC TÊN FILE CHO CẢ LÔ ẢNH
 *
 * Bản cũ quét toàn bộ folder cho từng ảnh và làm việc đó bên trong script
 * lock, nên album nhiều ảnh bị nghẽn và timeout lock. Bản này giữ một bộ
 * đếm trong ScriptProperties, chỉ quét folder đúng một lần đầu tiên.
 */
function reserveNextPhotoFileNames_(
  folder,
  count
) {
  return withScriptLock(
    function() {
      const properties =
        PropertiesService
          .getScriptProperties();

      const key =
        "photo_seq_" +
        folder.getId();

      let current =
        Number(
          properties.getProperty(
            key
          ) || 0
        );

      if (!current) {
        current =
          scanHighestPhotoIndex_(
            folder
          );
      }

      const names =
        [];

      for (
        let index = 1;
        index <= count;
        index++
      ) {
        names.push(
          String(
            current + index
          ).padStart(
            2,
            "0"
          ) +
          ".jpg"
        );
      }

      properties.setProperty(
        key,
        String(
          current + count
        )
      );

      return names;
    }
  );
}


function scanHighestPhotoIndex_(
  folder
) {
  const files =
    folder.getFiles();

  let highest =
    0;

  while (
    files.hasNext()
  ) {
    const file =
      files.next();

    const match =
      String(
        file.getName() || ""
      ).match(
        /^(\d+)\.jpe?g$/i
      );

    if (match) {
      highest =
        Math.max(
          highest,
          Number(
            match[1]
          )
        );
    } else {
      highest++;
    }
  }

  return highest;
}


/**
 * Giữ lại cho tương thích ngược.
 */
function buildNextPhotoFileName(
  folder
) {
  return reserveNextPhotoFileNames_(
    folder,
    1
  )[0];
}


/**
 * GHI SHEET MỘT LẦN
 *
 * Không động vào cột I.
 */
function updateInspectionRowOnce(
  device,
  pin,
  parts,
  ktvName,
  folderUrl
) {
  return withScriptLock(
    function() {
      const sheet =
        device.sheet;

      const sheetName =
        device.sheetName ||
        sheet.getName();

      const row =
        device.row;

      sheet
        .getRange(
          row,
          7
        )
        .setValue(
          "Đã kiểm định"
        );

      if (sheetName === "Macbook") {
        sheet
          .getRange(
            row,
            4
          )
          .setValue(
            parts || ""
          );

        sheet
          .getRange(
            row,
            9
          )
          .setValue(
            ktvName
          );

        sheet
          .getRange(
            row,
            10
          )
          .setValue(
            folderUrl
          );

        SpreadsheetApp.flush();
        return;
      }

      sheet
        .getRange(
          row,
          8
        )
        .setValue(
          pin
        );

      /*
       * Không có setValue cho cột I.
       */

      sheet
        .getRange(
          row,
          10
        )
        .setValue(
          ktvName
        );

      sheet
        .getRange(
          row,
          11
        )
        .setValue(
          folderUrl
        );

      sheet
        .getRange(
          row,
          12
        )
        .setValue(
          parts || ""
        );

      appendIphoneInspectionLogOnce(
        device,
        pin,
        ktvName
      );

      SpreadsheetApp.flush();
    }
  );
}

function appendIphoneInspectionLogOnce(
  device,
  pin,
  ktvName
) {
  return withSpreadsheetRetry(
    function() {
      const now =
        new Date();

      const logSpreadsheet =
        SpreadsheetApp.openById(
          IPHONE_INSPECTION_LOG_SPREADSHEET_ID
        );

      const logSheet =
        logSpreadsheet
          .getSheets()[0];

      const existingRow =
        findIphoneInspectionLogRow_(
          logSheet,
          now,
          ktvName,
          device.fullImei,
          pin
        );

      if (existingRow) {
        logSheet
          .getRange(
            existingRow,
            2,
            1,
            4
          )
          .setValues([[
            ktvName,
            device.phoneName,
            device.fullImei,
            pin
          ]]);
      } else {
        logSheet.appendRow([now, ktvName, device.phoneName, device.fullImei, pin]);
      }

      SpreadsheetApp.flush();
    }
  );
}

function findIphoneInspectionLogRow_(
  logSheet,
  dateValue,
  ktvName,
  imei,
  pin
) {
  const lastRow =
    logSheet.getLastRow();

  if (lastRow < 2) {
    return 0;
  }

  const displayValues =
    logSheet
      .getRange(
        2,
        1,
        lastRow - 1,
        5
      )
      .getDisplayValues();

  const key =
    buildIphoneInspectionLogKey_(
      dateValue,
      ktvName,
      imei,
      pin
    );

  for (
    let index = 0;
    index < displayValues.length;
    index++
  ) {
    const row =
      displayValues[index];

    if (
      buildIphoneInspectionLogKey_(
        row[0],
        row[1],
        row[3],
        row[4]
      ) === key
    ) {
      return index + 2;
    }
  }

  return 0;
}

function buildIphoneInspectionLogKey_(
  dateValue,
  ktvName,
  imei,
  pin
) {
  return [
    formatIphoneInspectionLogDateKey_(
      dateValue
    ),
    normalizeLogText_(
      ktvName
    ),
    normalizeLogImei_(
      imei
    ),
    normalizeLogText_(
      pin
    )
  ].join("|");
}

function formatIphoneInspectionLogDateKey_(
  value
) {
  if (
    Object.prototype.toString.call(value) === "[object Date]" &&
    !isNaN(value.getTime())
  ) {
    return Utilities.formatDate(
      value,
      Session.getScriptTimeZone() || "Asia/Ho_Chi_Minh",
      "dd/MM/yyyy"
    );
  }

  const text =
    String(
      value || ""
    ).trim();

  const match =
    text.match(
      /(\d{1,2})\/(\d{1,2})\/(\d{4})/
    );

  if (!match) {
    return text;
  }

  return (
    String(match[1]).padStart(2, "0") +
    "/" +
    String(match[2]).padStart(2, "0") +
    "/" +
    match[3]
  );
}

function normalizeLogText_(
  value
) {
  return String(
    value || ""
  )
    .trim()
    .toLowerCase()
    .replace(/\s+/g, " ");
}

function normalizeLogImei_(
  value
) {
  return String(
    value || ""
  ).replace(
    /\D/g,
    ""
  );
}

function repairIphoneInspectionLogDuplicates(
  deleteForReal
) {
  const dryRun =
    deleteForReal !== true;

  const logSpreadsheet =
    SpreadsheetApp.openById(
      IPHONE_INSPECTION_LOG_SPREADSHEET_ID
    );

  const logSheet =
    logSpreadsheet
      .getSheets()[0];

  const lastRow =
    logSheet.getLastRow();

  if (lastRow < 2) {
    const emptyResult = {
      dryRun:
        dryRun,

      totalRows:
        Math.max(
          lastRow - 1,
          0
        ),

      duplicateRows:
        []
    };

    Logger.log(
      JSON.stringify(
        emptyResult
      )
    );

    return emptyResult;
  }

  const values =
    logSheet
      .getRange(
        2,
        1,
        lastRow - 1,
        5
      )
      .getDisplayValues();

  const seen = {};
  const duplicateRows = [];

  values.forEach(
    function(row, index) {
      const rowNumber =
        index + 2;

      const key =
        buildIphoneInspectionLogKey_(
          row[0],
          row[1],
          row[3],
          row[4]
        );

      if (!key || key === "|||") {
        return;
      }

      if (seen[key]) {
        duplicateRows.push(
          rowNumber
        );
        return;
      }

      seen[key] =
        rowNumber;
    }
  );

  if (
    !dryRun &&
    duplicateRows.length
  ) {
    deleteRowsBottomUp_(
      logSheet,
      duplicateRows
    );
    SpreadsheetApp.flush();
  }

  const result = {
    dryRun:
      dryRun,

    totalRows:
      values.length,

    duplicateCount:
      duplicateRows.length,

    duplicateRows:
      duplicateRows
  };

  Logger.log(
    JSON.stringify(
      result
    )
  );

  return result;
}

function previewRepairIphoneInspectionLogDuplicates() {
  return repairIphoneInspectionLogDuplicates(
    false
  );
}

function runRepairIphoneInspectionLogDuplicatesNow() {
  return repairIphoneInspectionLogDuplicates(
    true
  );
}

function deleteRowsBottomUp_(
  sheet,
  rowNumbers
) {
  rowNumbers
    .slice()
    .sort(function(left, right) {
      return right - left;
    })
    .forEach(function(rowNumber) {
      sheet.deleteRow(
        rowNumber
      );
    });
}


/**
 * FOLDER
 */
function getOrCreateFolder(
  parentId,
  folderName
) {
  return withScriptLock(
    function() {
      const parent =
        DriveApp
          .getFolderById(
            parentId
          );

      const folders =
        parent
          .getFoldersByName(
            folderName
          );

      if (
        folders.hasNext()
      ) {
        return folders.next();
      }

      return parent.createFolder(
        folderName
      );
    }
  );
}


/**
 * PROPERTY CHỐNG TRÙNG
 */
function claimOnce(
  key
) {
  return withScriptLock(
    function() {
      const properties =
        PropertiesService
          .getScriptProperties();

      if (
        properties
          .getProperty(
            key
          )
      ) {
        return false;
      }

      properties.setProperty(
        key,
        String(
          Date.now()
        )
      );

      return true;
    }
  );
}

function albumInspectionContextKey_(
  mediaGroupId
) {
  return (
    "album_context_" +
    safeKey(
      mediaGroupId
    )
  );
}

function readAlbumInspectionContext_(
  cache,
  mediaGroupId
) {
  if (!mediaGroupId) {
    return null;
  }

  const raw =
    cache.get(
      albumInspectionContextKey_(
        mediaGroupId
      )
    );

  if (!raw) {
    return null;
  }

  try {
    return JSON.parse(
      raw
    );
  } catch (
    error
  ) {
    return null;
  }
}

function saveAlbumInspectionContext_(
  cache,
  mediaGroupId,
  context
) {
  if (!mediaGroupId) {
    return;
  }

  cache.put(
    albumInspectionContextKey_(
      mediaGroupId
    ),
    JSON.stringify(
      context
    ),
    21600
  );
}


/**
 * SCRIPT LOCK
 *
 * timeoutMs mặc định 30s thay vì 10s: album 10 ảnh gửi cùng lúc tạo ra
 * nhiều execution song song, 10s là quá ngắn và làm rơi ảnh.
 */
function withScriptLock(
  callback,
  timeoutMs
) {
  const lock =
    LockService
      .getScriptLock();

  let hasLock =
    false;

  try {
    lock.waitLock(
      Number(
        timeoutMs ||
        SCRIPT_LOCK_TIMEOUT_MS
      )
    );

    hasLock =
      true;

    return callback();

  } finally {
    if (hasLock) {
      lock.releaseLock();
    }
  }
}


function registerTelegramInspectionStatus(
  statusKey,
  chatId,
  details,
  captionMessageId
) {
  return withScriptLock(
    function() {
      const cache =
        CacheService
          .getScriptCache();

      let state =
        readTelegramInspectionStatus_(
          cache,
          statusKey
        );

      if (!state) {
        state = {
          chatId:
            chatId,

          messageId:
            null,

          lastRegisteredAt:
            0,

          reactedAt:
            0,

          captionMessageId:
            null,

          details:
            details || {}
        };
      }

      state.chatId =
        state.chatId ||
        chatId;

      state.details =
        details ||
        state.details ||
        {};

      state.captionMessageId =
        captionMessageId ||
        state.captionMessageId ||
        null;

      state.lastRegisteredAt =
        Date.now();

      if (!state.messageId) {
        const sent =
          sendTelegramMessage(
            state.chatId,
            renderTelegramInspectionStatus_(
              state
            )
          );

        state.messageId =
          sent.message_id;
      }

      saveTelegramInspectionStatus_(
        cache,
        statusKey,
        state
      );

      return state;
    }
  );
}


function readTelegramInspectionStatus_(
  cache,
  statusKey
) {
  const saved =
    cache.get(
      statusKey
    );

  if (!saved) {
    return null;
  }

  try {
    return JSON.parse(
      saved
    );
  } catch (
    error
  ) {
    return null;
  }
}


function saveTelegramInspectionStatus_(
  cache,
  statusKey,
  state
) {
  cache.put(
    statusKey,
    JSON.stringify(
      state
    ),
    21600
  );
}


function renderTelegramInspectionStatus_(
  state
) {
  const details =
    state.details || {};

  const header =
    "✅ ĐÃ KIỂM ĐỊNH MÁY THÀNH CÔNG!\n\n" +
    "👤 KTV: " +
    String(
      details.ktvName || ""
    ) +
    "\n" +
    "📱 Dòng máy: " +
    String(
      details.phoneName || ""
    ) +
    "\n";

  if (details.isMacCaption) {
    return (
      header +
      "🔑 Serial: " +
      String(
        details.imei || ""
      ) +
      "\n" +
      "⚙️ Thông số: " +
      String(
        details.parts || ""
      ) +
      "\n" +
      "📁 Folder ảnh: Đã lưu Drive!"
    );
  }

  return (
    header +
      "🔑 IMEI: " +
      String(
        details.imei || ""
      ) +
      "\n" +
      "📲 iOS: " +
      String(
        details.os || ""
      ) +
      "\n" +
      "🔋 Pin: " +
      String(
        details.pin || ""
      ) +
      "\n" +
      "🛠️ Linh kiện: " +
      String(
        details.parts || ""
      ) +
      "\n" +
    "📁 Folder ảnh: Đã lưu Drive!"
  );
}

function withSpreadsheetRetry(
  operation
) {
  let lastError = null;

  for (
    let attempt = 0;
    attempt < 3;
    attempt++
  ) {
    try {
      return operation();
    } catch (
      error
    ) {
      lastError =
        error;

      if (
        !isTransientSpreadsheetError(
          error
        ) ||
        attempt === 2
      ) {
        throw error;
      }

      Utilities.sleep(
        400 * (attempt + 1)
      );
    }
  }

  throw lastError;
}

function isTransientSpreadsheetError(
  error
) {
  const message =
    String(
      (
        error &&
        error.message
      ) ||
      error ||
      ""
    );

  return /Service Spreadsheets failed|Internal error|Backend Error|timed out/i.test(
    message
  );
}


/**
 * TELEGRAM
 */
function sendTelegramMessage(
  chatId,
  text,
  keyboard
) {
  const payload = {
    chat_id:
      chatId,

    text:
      text
  };

  if (keyboard) {
    payload.reply_markup =
      keyboard;
  }

  return callTelegramApi(
    "sendMessage",
    payload
  );
}


function editTelegramMessage(
  chatId,
  messageId,
  text
) {
  return callTelegramApi(
    "editMessageText",
    {
      chat_id:
        chatId,

      message_id:
        messageId,

      text:
        text
    }
  );
}


function sendReaction(
  chatId,
  messageId,
  emoji
) {
  if (
    !chatId ||
    !messageId
  ) {
    console.warn(
      "Bỏ qua Telegram reaction: thiếu chatId hoặc messageId."
    );

    return {
      ok:
        false,

      skipped:
        true
    };
  }

  return callTelegramApi(
    "setMessageReaction",
    {
      chat_id:
        chatId,

      message_id:
        messageId,

      reaction: [
        {
          type:
            "emoji",

          emoji:
            emoji
        }
      ]
    }
  );
}


function callTelegramApi(
  method,
  payload
) {
  const response =
    UrlFetchApp.fetch(
      "https://api.telegram.org/bot" +
      BOT_TOKEN +
      "/" +
      method,
      {
        method:
          "post",

        contentType:
          "application/json",

        payload:
          JSON.stringify(
            payload
          ),

        muteHttpExceptions:
          true
      }
    );

  const result =
    JSON.parse(
      response
        .getContentText()
    );

  if (!result.ok) {
    throw new Error(
      "Telegram API lỗi: " +
      (
        result.description ||
        response
          .getContentText()
      )
    );
  }

  return result.result;
}


/**
 * HÀM PHỤ
 */
function safeKey(
  value
) {
  return String(
    value || ""
  ).replace(
    /[^a-zA-Z0-9_-]/g,
    ""
  );
}


function sanitizeFolderName(
  value
) {
  return String(
    value || ""
  )
    .replace(
      /[\\/:*?"<>|]/g,
      "-"
    )
    .trim();
}


/**
 * CÀI WEBHOOK TELEGRAM TRỰC TIẾP VÀO APPS SCRIPT
 *
 * Cách dùng:
 * - Deploy Apps Script dạng Web app.
 * - Dán Web app URL vào APPS_SCRIPT_WEB_APP_URL ở đầu file.
 * - Chạy setAppsScriptWebhook().
 * - Hoặc truyền thủ công nếu muốn:
 *   setAppsScriptWebhook("https://script.google.com/macros/s/.../exec")
 */
function setAppsScriptWebhook(
  webAppUrl
) {
  const webhookUrl =
    getAppsScriptWebhookUrl(
      webAppUrl
    );

  const deleteResponse =
    UrlFetchApp.fetch(
      "https://api.telegram.org/bot" +
      BOT_TOKEN +
      "/deleteWebhook?drop_pending_updates=true",
      {
        method:
          "post",

        muteHttpExceptions:
          true
      }
    );

  Logger.log(
    "DELETE: " +
    deleteResponse
      .getContentText()
  );

  Utilities.sleep(
    1500
  );

  const setResponse =
    UrlFetchApp.fetch(
      "https://api.telegram.org/bot" +
      BOT_TOKEN +
      "/setWebhook",
      {
        method:
          "post",

        contentType:
          "application/json",

        payload:
          JSON.stringify({
            url:
              webhookUrl,

            allowed_updates: [
              "message"
            ],

            drop_pending_updates:
              true,

            max_connections:
              20
          }),

        muteHttpExceptions:
          true
      }
    );

  Logger.log(
    "SET: " +
    setResponse
      .getContentText()
  );
}


/**
 * CÀI WEBHOOK TELEGRAM QUA VERCEL
 *
 * Cách dùng:
 * - Vercel env:
 *   TELEGRAM_WEBAPP_SECRET = secret đang dùng
 *   TELEGRAM_APPS_SCRIPT_WEBHOOK_URL = link Apps Script /exec
 * - Apps Script Properties:
 *   ONEWAY_WEBAPP_URL = https://onewaybiennhan.vercel.app
 *   TELEGRAM_WEBAPP_SECRET = secret đang dùng
 * - Chạy setVercelTelegramWebhook()
 */
function setVercelTelegramWebhook(
  webappOrigin
) {
  const config =
    getWebappBridgeConfig_();

  const origin =
    String(
      webappOrigin ||
      config.webappUrl ||
      ""
    ).replace(
      /\/+$/,
      ""
    );

  if (
    !origin ||
    !config.secret
  ) {
    throw new Error(
      "Thiếu ONEWAY_WEBAPP_URL hoặc TELEGRAM_WEBAPP_SECRET trong Script Properties."
    );
  }

  const webhookUrl =
    origin +
    "/api/integrations/telegram/webhook?secret=" +
    encodeURIComponent(
      config.secret
    );

  const deleteResponse =
    UrlFetchApp.fetch(
      "https://api.telegram.org/bot" +
      BOT_TOKEN +
      "/deleteWebhook?drop_pending_updates=true",
      {
        method:
          "post",

        muteHttpExceptions:
          true
      }
    );

  Logger.log(
    "DELETE: " +
    deleteResponse
      .getContentText()
  );

  Utilities.sleep(
    1500
  );

  const setResponse =
    UrlFetchApp.fetch(
      "https://api.telegram.org/bot" +
      BOT_TOKEN +
      "/setWebhook",
      {
        method:
          "post",

        contentType:
          "application/json",

        payload:
          JSON.stringify({
            url:
              webhookUrl,

            allowed_updates: [
              "message"
            ],

            drop_pending_updates:
              true,

            max_connections:
              20
          }),

        muteHttpExceptions:
          true
      }
    );

  Logger.log(
    "SET VERCEL: " +
    setResponse
      .getContentText()
  );
}


/**
 * TEST VERCEL → APPS SCRIPT FORWARD
 *
 * Chạy hàm này trong Apps Script sau khi Vercel deploy xong.
 * Nếu log trả 200 + {"ok":true} thì Vercel endpoint đã nhận và forward
 * về Apps Script được.
 */
function testVercelTelegramWebhookForward(
  webappOrigin
) {
  const config =
    getWebappBridgeConfig_();

  const origin =
    String(
      webappOrigin ||
      config.webappUrl ||
      ""
    ).replace(
      /\/+$/,
      ""
    );

  if (
    !origin ||
    !config.secret
  ) {
    throw new Error(
      "Thiếu ONEWAY_WEBAPP_URL hoặc TELEGRAM_WEBAPP_SECRET trong Script Properties."
    );
  }

  const response =
    UrlFetchApp.fetch(
      origin +
        "/api/integrations/telegram/webhook?secret=" +
        encodeURIComponent(
          config.secret
        ),
      {
        method:
          "post",

        contentType:
          "application/json",

        payload:
          JSON.stringify({
            update_id:
              Date.now(),

            message: {
              message_id:
                1,

              date:
                Math.floor(
                  Date.now() / 1000
                ),

              chat: {
                id:
                  0,

                type:
                  "private"
              },

              text:
                "vercel-forward-health-check"
            }
          }),

        muteHttpExceptions:
          true
      }
    );

  Logger.log(
    "VERCEL FORWARD TEST HTTP " +
    response.getResponseCode() +
    ": " +
    response.getContentText()
  );
}


function checkTelegramWebhook() {
  const response =
    UrlFetchApp.fetch(
      "https://api.telegram.org/bot" +
      BOT_TOKEN +
      "/getWebhookInfo"
    );

  Logger.log(
    response
      .getContentText()
  );
}


function getAppsScriptWebhookUrl(
  webAppUrl
) {
  const url =
    String(
      webAppUrl ||
      APPS_SCRIPT_WEB_APP_URL ||
      ScriptApp
        .getService()
        .getUrl() ||
      ""
    )
      .trim();

  if (
    !url ||
    url.indexOf(
      "https://script.google.com/macros/s/"
    ) !== 0 ||
    url.indexOf(
      "/exec"
    ) === -1
  ) {
    throw new Error(
      "Không tìm thấy Apps Script Web App URL. Deploy Web app rồi chạy lại setAppsScriptWebhook(url)."
    );
  }

  return url;
}

/**
 * BRIDGE SANG WEBAPP NEXT.JS
 *
 * Script Properties required:
 * - ONEWAY_WEBAPP_URL = https://onewaybiennhan.vercel.app
 * - TELEGRAM_WEBAPP_SECRET = giống env TELEGRAM_WEBAPP_SECRET trên Vercel
 */
function getWebappBridgeConfig_() {
  const props =
    PropertiesService
      .getScriptProperties();

  return {
    webappUrl:
      String(
        props.getProperty(
          "ONEWAY_WEBAPP_URL"
        ) || ""
      ).replace(/\/+$/, ""),

    secret:
      String(
        props.getProperty(
          "TELEGRAM_WEBAPP_SECRET"
        ) || ""
      )
  };
}

function parseJsonResponse_(
  response
) {
  try {
    return JSON.parse(
      response.getContentText() ||
      "{}"
    );
  } catch (
    error
  ) {
    return {};
  }
}


function queueTelegramPhotoToWebapp(
  blob,
  caption,
  ktvName,
  telegramUsername,
  folderUrl,
  fileName,
  deviceName,
  inspectionId
) {
  const config =
    getWebappBridgeConfig_();

  if (
    !config.webappUrl ||
    !config.secret
  ) {
    console.warn(
      "Bỏ qua webapp bridge: thiếu ONEWAY_WEBAPP_URL hoặc TELEGRAM_WEBAPP_SECRET."
    );

    return {
      ok: false,
      skipped: true
    };
  }

  try {
    const response =
      UrlFetchApp.fetch(
        config.webappUrl +
          "/api/integrations/telegram/inspection-media",
        {
          method: "post",

          headers: {
            Authorization:
              "Bearer " +
              config.secret
          },

          payload: {
            file:
              blob,

            caption:
              String(
                caption ||
                ""
              ),

            ktvName:
              String(
                ktvName ||
                ""
              ),

            telegramUsername:
              String(
                telegramUsername ||
                ""
              ),

            folderUrl:
              String(
                folderUrl ||
                ""
              ),

            deviceName:
              String(
                deviceName ||
                ""
              ),

            fileName:
              String(
                fileName ||
                ""
              ),

            inspectionId:
              String(
                inspectionId ||
                ""
              ),

            uploadedToDrive:
              "true"
          },

          muteHttpExceptions:
            true
        }
      );

    const status =
      response.getResponseCode();

    if (
      status < 200 ||
      status >= 300
    ) {
      console.warn(
        "Webapp bridge lỗi HTTP " +
        status +
        ": " +
        response.getContentText()
      );
    }

    return {
      ok:
        status >= 200 &&
        status < 300,

      status:
        status
    };

  } catch (error) {
    console.warn(
      "Webapp bridge exception: " +
      error
    );

    return {
      ok: false,
      error: String(error)
    };
  }
}


function notifyTelegramInspectionToWebapp(
  caption,
  ktvName,
  telegramUsername,
  folderUrl,
  deviceName
) {
  const config =
    getWebappBridgeConfig_();

  if (
    !config.webappUrl ||
    !config.secret
  ) {
    console.warn(
      "Bỏ qua webapp bridge metadata: thiếu ONEWAY_WEBAPP_URL hoặc TELEGRAM_WEBAPP_SECRET."
    );

    return {
      ok: false,
      skipped: true
    };
  }

  try {
    const response =
      UrlFetchApp.fetch(
        config.webappUrl +
          "/api/integrations/telegram/inspection-media",
        {
          method: "post",

          headers: {
            Authorization:
              "Bearer " +
              config.secret
          },

          payload: {
            caption:
              String(
                caption ||
                ""
              ),

            ktvName:
              String(
                ktvName ||
                ""
              ),

            telegramUsername:
              String(
                telegramUsername ||
                ""
              ),

            folderUrl:
              String(
                folderUrl ||
                ""
              ),

            deviceName:
              String(
                deviceName ||
                ""
              )
          },

          muteHttpExceptions:
            true
        }
      );

    const status =
      response.getResponseCode();

    if (
      status < 200 ||
      status >= 300
    ) {
      console.warn(
        "Webapp bridge metadata lỗi HTTP " +
        status +
        ": " +
        response.getContentText()
      );
    }

    const body =
      parseJsonResponse_(
        response
      );

    return {
      ok:
        status >= 200 &&
        status < 300,

      status:
        status,

      inspectionId:
        body && body.inspectionId
          ? String(
              body.inspectionId
            )
          : ""
    };

  } catch (error) {
    console.warn(
      "Webapp bridge metadata exception: " +
      error
    );

    return {
      ok: false,
      error: String(error)
    };
  }
}


function setupWebappBridgeProperties() {
  PropertiesService
    .getScriptProperties()
    .setProperties({
      ONEWAY_WEBAPP_URL:
        "https://onewaybiennhan.vercel.app",

      TELEGRAM_WEBAPP_SECRET:
        "PASTE_TELEGRAM_WEBAPP_SECRET_VAO_DAY"
    });
}


/**
 * DỌN SCRIPT PROPERTIES CŨ
 *
 * Mặc định chỉ chạy thử và log danh sách sẽ xoá:
 *   clearLegacyScriptProperties()
 *
 * Xoá rác cũ, giữ lại config bridge sang webapp:
 *   clearLegacyScriptProperties(true)
 *
 * Xoá sạch toàn bộ, kể cả ONEWAY_WEBAPP_URL và TELEGRAM_WEBAPP_SECRET:
 *   clearLegacyScriptProperties(true, true)
 *
 * LƯU Ý: xoá key photo_seq_* sẽ làm bộ đếm tên file quét lại folder từ đầu.
 * Xoá key photo_done_* sẽ mất dấu chống upload trùng của ảnh cũ.
 */
function clearLegacyScriptProperties(
  deleteForReal,
  includeBridgeConfig
) {
  const dryRun =
    deleteForReal !== true;

  const deleteBridgeConfig =
    includeBridgeConfig === true;

  const protectedKeys = {
    ONEWAY_WEBAPP_URL:
      true,

    TELEGRAM_WEBAPP_SECRET:
      true
  };

  const properties =
    PropertiesService
      .getScriptProperties();

  const allProperties =
    properties
      .getProperties();

  const deleted =
    [];

  const kept =
    [];

  Object.keys(
    allProperties
  ).forEach(
    function(key) {
      const isProtected =
        protectedKeys[key] === true;

      if (
        isProtected &&
        !deleteBridgeConfig
      ) {
        kept.push(
          key
        );

        return;
      }

      deleted.push(
        key
      );

      if (!dryRun) {
        properties
          .deleteProperty(
            key
          );
      }
    }
  );

  const result = {
    dryRun:
      dryRun,

    deletedCount:
      deleted.length,

    deleted:
      deleted,

    kept:
      kept
  };

  Logger.log(
    JSON.stringify(
      result,
      null,
      2
    )
  );

  return result;
}
