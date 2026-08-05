// Installed by the 2D Assets Pipeline tool. Reads the sidecar "<name>.import.json"
// next to each exported PNG and applies the pipeline's import settings automatically
// whenever Unity (re)imports the texture. Re-exporting from the pipeline overwrites
// the PNG + JSON and Unity picks the changes up on the next import.
using System;
using System.IO;
using UnityEditor;
using UnityEngine;

public class AssetPipelineImporter : AssetPostprocessor
{
    [Serializable]
    private class ImportSettings
    {
        public float pixelsPerUnit = 100f;
        // left, bottom, right, top — matches TextureImporter.spriteBorder (X=left, Y=bottom, Z=right, W=top)
        public float[] spriteBorder;
        public string spriteMode = "Single";   // Single | Sliced | Multiple
        public string filterMode = "Point";    // Point | Bilinear
        public string wrapMode = "Clamp";      // Clamp | Repeat
        public string meshType = "FullRect";   // FullRect | Tight
        public int sheetRows = 0;
        public int sheetCols = 0;
    }

    private void OnPreprocessTexture()
    {
        string jsonPath = Path.ChangeExtension(assetPath, null) + ".import.json";
        if (!File.Exists(jsonPath))
            return;

        ImportSettings settings;
        try
        {
            settings = JsonUtility.FromJson<ImportSettings>(File.ReadAllText(jsonPath));
        }
        catch (Exception e)
        {
            Debug.LogWarning($"AssetPipelineImporter: could not parse {jsonPath}: {e.Message}");
            return;
        }

        var importer = (TextureImporter)assetImporter;
        importer.textureType = TextureImporterType.Sprite;
        importer.spritePixelsPerUnit = settings.pixelsPerUnit;
        importer.alphaIsTransparency = true;
        importer.mipmapEnabled = false;

        importer.filterMode = settings.filterMode == "Bilinear" ? FilterMode.Bilinear : FilterMode.Point;
        importer.wrapMode = settings.wrapMode == "Repeat" ? TextureWrapMode.Repeat : TextureWrapMode.Clamp;

        bool isSheet = settings.spriteMode == "Multiple" && settings.sheetRows > 0 && settings.sheetCols > 0;
        importer.spriteImportMode = isSheet ? SpriteImportMode.Multiple : SpriteImportMode.Single;

        if (settings.spriteBorder != null && settings.spriteBorder.Length == 4)
        {
            importer.spriteBorder = new Vector4(
                settings.spriteBorder[0], settings.spriteBorder[1],
                settings.spriteBorder[2], settings.spriteBorder[3]);
        }

        var textureSettings = new TextureImporterSettings();
        importer.ReadTextureSettings(textureSettings);
        textureSettings.spriteMeshType = settings.meshType == "Tight" ? SpriteMeshType.Tight : SpriteMeshType.FullRect;
        importer.SetTextureSettings(textureSettings);

        if (isSheet)
            EditorApplication.delayCall += () => SliceSheet(assetPath, settings);
    }

    // Grid-slice a sprite sheet once the texture itself has been imported (we need
    // its pixel dimensions, which are unavailable during OnPreprocessTexture).
    private static void SliceSheet(string path, ImportSettings settings)
    {
        var texture = AssetDatabase.LoadAssetAtPath<Texture2D>(path);
        var importer = AssetImporter.GetAtPath(path) as TextureImporter;
        if (texture == null || importer == null)
            return;

        int cellW = texture.width / settings.sheetCols;
        int cellH = texture.height / settings.sheetRows;
        string baseName = Path.GetFileNameWithoutExtension(path);

        var metas = new SpriteMetaData[settings.sheetRows * settings.sheetCols];
        int i = 0;
        for (int row = 0; row < settings.sheetRows; row++)
        {
            for (int col = 0; col < settings.sheetCols; col++)
            {
                metas[i] = new SpriteMetaData
                {
                    name = $"{baseName}_{i}",
                    // SpriteMetaData rects are bottom-left origin; row 0 = top row of the sheet
                    rect = new Rect(col * cellW, texture.height - (row + 1) * cellH, cellW, cellH),
                    alignment = (int)SpriteAlignment.Center,
                };
                i++;
            }
        }
#pragma warning disable 0618
        importer.spritesheet = metas;
#pragma warning restore 0618
        importer.SaveAndReimport();
    }
}
