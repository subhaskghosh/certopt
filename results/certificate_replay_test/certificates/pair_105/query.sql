SELECT
  p.firstname,
  p.lastname,
  ad.city,
  ad.state
FROM PERSON AS P
LEFT JOIN ADDRESS AS AD
  ON ad.personid = p.personid