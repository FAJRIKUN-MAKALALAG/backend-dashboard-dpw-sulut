-- Menambah kolom urutan pada tabel members (aman: tidak menghapus / mengubah data yang ada).
-- Jalankan sekali di Supabase -> SQL Editor.
ALTER TABLE public.members ADD COLUMN IF NOT EXISTS urutan integer;
CREATE INDEX IF NOT EXISTS idx_members_urutan ON public.members (urutan);
