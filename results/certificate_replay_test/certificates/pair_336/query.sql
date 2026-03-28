SELECT DISTINCT
  p.email AS EMAIL
FROM PERSON AS P
LEFT JOIN PERSON AS DP
  ON dp.email = p.email
WHERE
  dp.id <> p.id